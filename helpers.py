from datetime import datetime, timedelta
from db import get_db_connection


def uid_for_user(loginname):
    """登录用户名 -> 绑定的 IC 卡 uid；未绑定返回 None。"""
    conn = get_db_connection()
    row = conn.execute("SELECT uid FROM users WHERE loginname=?", (loginname,)).fetchone()
    conn.close()
    return row['uid'] if row else None


def compute_week_key(dt):
    """
    计算提交归属的 ISO week key。
    周六/周日 -> 归属下周
    周一(<=18点) -> 归属当周
    其他时间 -> 拒绝（返回 None）
    """
    weekday = dt.weekday()  # 0=Mon, 5=Sat, 6=Sun
    hour = dt.hour
    if weekday == 5 or weekday == 6:  # 周六、周日 -> 下周
        next_mon = dt + timedelta(days=(7 - weekday))
        return next_mon.strftime('%Y%W')
    elif weekday == 0 and hour <= 18:  # 周一(<=18点) -> 当周
        return dt.strftime('%Y%W')
    else:
        return None


def save_weekly_plan(uid, week_key, items):
    """保存一周计划：清洗+校验后，单事务 upsert 父行(weekly_plans)并全量替换 items。

    - items: 字符串列表；去首尾空白后丢弃空串。
    - 限制：≤5 条、每条 ≤30 字，超出抛 ValueError（由调用方捕获并给出友好提示）。
    - 父行 content 存 join 后的文本，供 monthly summary 的 LLM 文本构建沿用。
    """
    cleaned = [s.strip() for s in items if isinstance(s, str) and s.strip()]
    if len(cleaned) > 5:
        raise ValueError("每周计划条目不能超过 5 条")
    for s in cleaned:
        if len(s) > 30:
            raise ValueError("每条计划不能超过 30 字")
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO weekly_plans (uid, week_key, content) VALUES (?, ?, ?)",
            (uid, week_key, "\n".join(cleaned)))
        conn.execute(
            "DELETE FROM weekly_plan_items WHERE uid=? AND week_key=?",
            (uid, week_key))
        for order, text in enumerate(cleaned):
            conn.execute(
                "INSERT INTO weekly_plan_items (uid, week_key, item_order, text) VALUES (?, ?, ?, ?)",
                (uid, week_key, order, text))
        conn.commit()
    finally:
        conn.close()
    return len(cleaned)


def current_completion_date():
    """日报归属日：now-6h（凌晨 0–6 点归到前一天），与签到/日报的 6 小时工作日一致。"""
    return (datetime.now() - timedelta(hours=6)).strftime('%Y-%m-%d')


def save_daily_completion(uid, review, todo):
    """保存当天日报（review 必填，todo 选填）；多次编辑只保留最新（INSERT OR REPLACE）。

    - review 去首尾空白后不能为空；review/todo 各 ≤100 字，超出抛 ValueError（由调用方给出友好提示）。
    - 归属日期 = current_completion_date()（now-6h），API 与 Web 共用，保证“可编辑格”与“落库日”一致。
    - 返回归属日期字符串。
    """
    review = (review or '').strip()
    todo = (todo or '').strip()
    if not review:
        raise ValueError("review 不能为空")
    if len(review) > 100:
        raise ValueError("review 不能超过100字")
    if len(todo) > 100:
        raise ValueError("todo 不能超过100字")
    date = current_completion_date()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_completions (uid, date, review, todo) VALUES (?, ?, ?, ?)",
            (uid, date, review, todo or None))
        conn.commit()
    finally:
        conn.close()
    # 异步：基于本次 review 让 AI 判定本周 weekly plan item 的完成情况（best-effort，不阻塞保存）
    import threading
    threading.Thread(
        target=_auto_complete_weekly_plan_items,
        args=(uid, review, date),
        daemon=True
    ).start()
    return date


def _auto_complete_weekly_plan_items(uid, review, date_str):
    """后台线程：用 review 让 AI 判定本周 open 的 weekly plan item 是否完成，命中则置 done。

    - week_key 取 date_str 所在周的周一 %Y%W（与 detail 页 this_key 一致）。
    - 单调：只把 status='open' 的项更新为 'done'（AND status='open' 保证幂等/并发安全）。
    - 整体 best-effort：任何异常都吞掉，绝不影响已提交的 daily save。
    """
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        monday = d - timedelta(days=d.weekday())
        week_key = monday.strftime('%Y%W')
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id, text FROM weekly_plan_items WHERE uid=? AND week_key=? AND status='open'",
                (uid, week_key)).fetchall()
            if not rows:
                return
            from llm import judge_completed_plan_items
            items = [{'id': r['id'], 'text': r['text']} for r in rows]
            done_ids = judge_completed_plan_items(review, items)
            for item_id in done_ids:
                conn.execute(
                    "UPDATE weekly_plan_items SET status='done', completed_date=?, completed_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status='open'",
                    (date_str, item_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def is_instance_expired(slot, instance_date):
    """活动实例是否已过结束时间：now >= instance_date(YYYY-MM-DD) + end_hour

    end_hour 为浮点（支持半点，如 6.5=6:30）。半点也可被 timedelta(hours=...) 直接接受。
    """
    base = datetime.strptime(instance_date, '%Y-%m-%d')
    end_dt = base + timedelta(hours=slot['end_hour'])
    return datetime.now() >= end_dt


def compute_upcoming_instances(slot, n=4):
    """根据 slot 类型计算接下来 n 个可用实例日期"""
    today = datetime.now().date()
    if slot['slot_type'] == 'one_time':
        if slot['specific_date'] and slot['specific_date'] >= today.strftime('%Y-%m-%d'):
            return [slot['specific_date']]
        return []
    else:  # recurring
        target_dow = slot['day_of_week']
        days_ahead = (target_dow - today.weekday()) % 7
        if days_ahead == 0 and datetime.now().hour >= slot['start_hour']:
            days_ahead = 7
        first_date = today + timedelta(days=days_ahead)
        return [(first_date + timedelta(weeks=i)).strftime('%Y-%m-%d') for i in range(n)]


def compute_summary_week_range(week_offset=0):
    """计算周摘要的日期范围和 week_key。week_offset=0 为最近一个完整周。"""
    today = datetime.now().date()
    current_monday = today - timedelta(days=today.weekday())
    summary_monday = current_monday - timedelta(weeks=1 + week_offset)
    summary_sunday = summary_monday + timedelta(days=6)
    next_monday = summary_monday + timedelta(weeks=1)
    return summary_monday, summary_sunday, summary_monday.strftime('%Y%W'), next_monday.strftime('%Y%W')


def generate_weekly_summary(week_offset=0):
    """生成某周的出勤/日报/周计划摘要，返回 list[dict]"""
    summary_monday, summary_sunday, _, next_week_key = compute_summary_week_range(week_offset)
    start_date = summary_monday.strftime('%Y-%m-%d')
    end_date = (summary_sunday + timedelta(days=1)).strftime('%Y-%m-%d')

    conn = get_db_connection()
    users = conn.execute("SELECT uid, name FROM users ORDER BY name").fetchall()

    att_rows = conn.execute(
        "SELECT uid, COUNT(DISTINCT date(timestamp)) as days FROM sign_ins "
        "WHERE timestamp >= ? AND timestamp < ? GROUP BY uid",
        (start_date + ' 00:00:00', end_date + ' 00:00:00')
    ).fetchall()
    att_map = {r['uid']: r['days'] for r in att_rows}

    comp_rows = conn.execute(
        "SELECT uid, COUNT(*) as cnt FROM daily_completions "
        "WHERE date >= ? AND date < ? GROUP BY uid",
        (start_date, end_date)
    ).fetchall()
    comp_map = {r['uid']: r['cnt'] for r in comp_rows}

    plan_rows = conn.execute(
        "SELECT uid FROM weekly_plans WHERE week_key = ?", (next_week_key,)
    ).fetchall()
    plan_set = set(r['uid'] for r in plan_rows)

    conn.close()

    summary_list = []
    for u in users:
        if u['uid'] not in att_map and u['uid'] not in comp_map and u['uid'] not in plan_set:
            continue  # inactive user
        summary_list.append({
            'uid': u['uid'],
            'name': u['name'],
            'onsite_days': att_map.get(u['uid'], 0),
            'completion_count': comp_map.get(u['uid'], 0),
            'has_weekly_plan': u['uid'] in plan_set
        })
    summary_list.sort(key=lambda x: x['onsite_days'], reverse=True)
    return summary_list


def compute_month_range(month_offset=0):
    """计算自然月的日期范围。month_offset=0 为上一自然月。"""
    today = datetime.now().date()
    # Target month: today's month minus (1 + offset) months
    target_month = today.month - (1 + month_offset)
    target_year = today.year
    while target_month <= 0:
        target_month += 12
        target_year -= 1
    month_key = f'{target_year}-{target_month:02d}'
    start_date = datetime(target_year, target_month, 1).date()
    # Next month first day (exclusive upper bound)
    next_month = target_month + 1
    next_year = target_year
    if next_month > 12:
        next_month = 1
        next_year += 1
    end_date = datetime(next_year, next_month, 1).date()
    return start_date, end_date, month_key


def generate_monthly_summary(month_offset=0):
    """生成自然月的出勤/日报/周计划摘要，返回 list[dict]"""
    start_date, end_date, month_key = compute_month_range(month_offset)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    # Compute week_keys whose Monday falls within this month
    week_keys = []
    d = start_date
    while d < end_date:
        if d.weekday() == 0:  # Monday
            week_keys.append(d.strftime('%Y%W'))
            d += timedelta(weeks=1)
        else:
            d += timedelta(days=1)

    conn = get_db_connection()
    users = conn.execute("SELECT uid, name FROM users ORDER BY name").fetchall()

    # Onsite days
    att_rows = conn.execute(
        "SELECT uid, COUNT(DISTINCT date(timestamp)) as days FROM sign_ins "
        "WHERE timestamp >= ? AND timestamp < ? GROUP BY uid",
        (start_str + ' 00:00:00', end_str + ' 00:00:00')
    ).fetchall()
    att_map = {r['uid']: r['days'] for r in att_rows}

    # Daily completion count + text
    comp_rows = conn.execute(
        "SELECT uid, COUNT(*) as cnt FROM daily_completions "
        "WHERE date >= ? AND date < ? GROUP BY uid",
        (start_str, end_str)
    ).fetchall()
    comp_map = {r['uid']: r['cnt'] for r in comp_rows}

    comp_text_rows = conn.execute(
        "SELECT uid, date, review, todo FROM daily_completions "
        "WHERE date >= ? AND date < ? ORDER BY date",
        (start_str, end_str)
    ).fetchall()
    comp_text_by_uid = {}
    for r in comp_text_rows:
        line = f"{r['date']}: 回顾:{r['review']}"
        if r['todo']:
            line += f" 计划:{r['todo']}"
        comp_text_by_uid.setdefault(r['uid'], []).append(line)

    # Weekly plan count + text
    wk_placeholders = ','.join(['?'] * len(week_keys)) if week_keys else "'__none__'"
    plan_rows = conn.execute(
        f"SELECT uid, COUNT(*) as cnt FROM weekly_plans "
        f"WHERE week_key IN ({wk_placeholders}) GROUP BY uid",
        week_keys
    ).fetchall() if week_keys else []
    plan_map = {r['uid']: r['cnt'] for r in plan_rows}

    plan_text_rows = conn.execute(
        f"SELECT uid, week_key, content FROM weekly_plans "
        f"WHERE week_key IN ({wk_placeholders}) ORDER BY week_key",
        week_keys
    ).fetchall() if week_keys else []
    plan_text_by_uid = {}
    for r in plan_text_rows:
        plan_text_by_uid.setdefault(r['uid'], []).append(f"Week {r['week_key']}: {r['content']}")

    # Cached LLM summaries
    summary_rows = conn.execute(
        "SELECT uid, summary, suggestion FROM monthly_summaries WHERE month_key = ?",
        (month_key,)
    ).fetchall()
    llm_map = {r['uid']: {'summary': r['summary'], 'suggestion': r['suggestion']} for r in summary_rows}

    conn.close()

    summary_list = []
    for u in users:
        uid = u['uid']
        if uid not in att_map and uid not in comp_map and uid not in plan_map:
            continue
        llm = llm_map.get(uid, {'summary': None, 'suggestion': None})
        summary_list.append({
            'uid': uid,
            'name': u['name'],
            'onsite_days': att_map.get(uid, 0),
            'completion_count': comp_map.get(uid, 0),
            'plan_count': plan_map.get(uid, 0),
            'daily_completions_text': '\n'.join(comp_text_by_uid.get(uid, [])),
            'weekly_plans_text': '\n'.join(plan_text_by_uid.get(uid, [])),
            'summary': llm['summary'],
            'suggestion': llm['suggestion']
        })
    summary_list.sort(key=lambda x: x['onsite_days'], reverse=True)
    return summary_list