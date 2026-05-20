from datetime import datetime, timedelta
from db import get_db_connection


def compute_week_key(dt):
    """
    计算提交归属的 ISO week key。
    周六/周日 -> 归属下周
    周一上午(<=12点) -> 归属当周
    其他时间 -> 拒绝（返回 None）
    """
    weekday = dt.weekday()  # 0=Mon, 5=Sat, 6=Sun
    hour = dt.hour
    if weekday == 5 or weekday == 6:  # 周六、周日 -> 下周
        next_mon = dt + timedelta(days=(7 - weekday))
        return next_mon.strftime('%Y%W')
    elif weekday == 0 and hour <= 12:  # 周一上午 -> 当周
        return dt.strftime('%Y%W')
    else:
        return None


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
        "SELECT uid, date, content FROM daily_completions "
        "WHERE date >= ? AND date < ? ORDER BY date",
        (start_str, end_str)
    ).fetchall()
    comp_text_by_uid = {}
    for r in comp_text_rows:
        comp_text_by_uid.setdefault(r['uid'], []).append(f"{r['date']}: {r['content']}")

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