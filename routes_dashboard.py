import sqlite3
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from datetime import datetime, timedelta
from db import get_db_connection
from auth import login_required, VALID_USERS
from helpers import compute_month_range, generate_monthly_summary, is_instance_expired, compute_week_key, save_weekly_plan, save_daily_completion, current_completion_date, uid_for_user

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@login_required
def index():
    conn = get_db_connection()

    # delete sign-ins by unknown users (after 3 days) to keep the table clean
    cutoff = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        'DELETE FROM sign_ins WHERE uid NOT IN (SELECT uid FROM users) AND timestamp < ?',
        (cutoff,)
    )
    conn.commit()

    sql = '''
        SELECT s.uid, MAX(s.timestamp) as last_time, u.name, u.loginname
        FROM sign_ins s
        LEFT JOIN users u ON s.uid = u.uid
        WHERE s.timestamp >= date('now','-180 days')
        GROUP BY s.uid
        ORDER BY last_time DESC
    '''
    records = conn.execute(sql).fetchall()

    # 可选登录名 = .users.txt(VALID_USERS) 中尚未绑定到任何卡片的（保证 1:1）
    bound_loginnames = {row['loginname'] for row in conn.execute(
        "SELECT loginname FROM users WHERE loginname IS NOT NULL").fetchall()}
    available_loginnames = sorted(VALID_USERS - bound_loginnames)

    # 一次性预约轮播数据
    today_str = datetime.now().strftime('%Y-%m-%d')
    carousel_slots = conn.execute(
        "SELECT * FROM booking_slots WHERE slot_type='one_time' AND status='active' AND specific_date >= ? ORDER BY specific_date ASC LIMIT 5",
        (today_str,)
    ).fetchall()
    carousel_data = []
    for slot in carousel_slots:
        # 跳过已过结束时间的一次性活动，首页只推尚未结束的
        if is_instance_expired(slot, slot['specific_date']):
            continue
        booked_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND instance_date=? AND status='active'",
            (slot['id'], slot['specific_date'])
        ).fetchone()[0]
        carousel_data.append(dict(slot=dict(slot), remaining=slot['capacity'] - booked_count))

    conn.close()
    return render_template('index.html', page='index', records=records,
                           carousel_data=carousel_data,
                           available_loginnames=available_loginnames)


@dashboard_bp.route('/bind', methods=['POST'])
@login_required
def bind():
    """绑定/更新一张卡：必须同时指定 loginname（未绑定的）和姓名。"""
    uid = (request.form.get('uid') or '').strip()
    name = (request.form.get('name') or '').strip()
    loginname = (request.form.get('loginname') or '').strip()

    if not uid:
        flash('缺少卡片 UID。')
        return redirect(url_for('dashboard.index'))
    if not loginname:
        flash('请选择登录名。')
        return redirect(url_for('dashboard.index'))
    if not name:
        flash('请输入姓名。')
        return redirect(url_for('dashboard.index'))
    if loginname not in VALID_USERS:
        flash('未知登录名，请从下拉列表选择。')
        return redirect(url_for('dashboard.index'))

    conn = get_db_connection()
    # 1:1：loginname 不能已绑到别的 uid（UNIQUE 约束兜底，这里给友好提示）
    owner = conn.execute("SELECT uid FROM users WHERE loginname=?", (loginname,)).fetchone()
    if owner and owner['uid'] != uid:
        flash(f'登录名 {loginname} 已绑定到其他卡片，请换一个。')
        conn.close()
        return redirect(url_for('dashboard.index'))

    try:
        row = conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone()
        if row:
            conn.execute("UPDATE users SET name=?, loginname=? WHERE uid=?",
                         (name, loginname, uid))
        else:
            conn.execute("INSERT INTO users (uid, name, loginname) VALUES (?, ?, ?)",
                         (uid, name, loginname))
        conn.commit()
        flash('绑定成功。')
    except sqlite3.IntegrityError:
        # 并发或被抢先绑定导致 loginname 唯一冲突
        flash('绑定失败：该登录名可能已被他人抢先绑定。')
    finally:
        conn.close()
    return redirect(url_for('dashboard.index'))


@dashboard_bp.route('/stats')
@login_required
def stats():
    conn = get_db_connection()

    # 计算上一个自然月的月份标签
    today = datetime.now()
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - timedelta(days=1)
    last_month_label = f"{last_day_last_month.strftime('%Y%m')}"

    # 1次查询：所有用户在180天内的 stats（JOIN users 表，GROUP BY uid）
    stats_query = '''
        SELECT
            s.uid,
            COALESCE(u.name, '未绑定') as name,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-15 days') THEN date(s.timestamp) END) as d15,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-60 days') THEN date(s.timestamp) END) as d60,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-180 days') THEN date(s.timestamp) END) as d180,
            COUNT(DISTINCT CASE WHEN strftime('%Y-%m',s.timestamp) = strftime('%Y-%m','now','-1 month') THEN date(s.timestamp) END) as last_month
        FROM sign_ins s
        LEFT JOIN users u ON s.uid = u.uid
        WHERE s.timestamp >= date('now','-180 days')
        GROUP BY s.uid
        ORDER BY d15 DESC
    '''
    stats_list = [dict(row) for row in conn.execute(stats_query).fetchall()]

    # 1次查询：全员56天热力图数据（GROUP BY uid）
    heatmap_dates = []
    today_date = datetime.now().date()
    for i in range(55, -1, -1):
        d = today_date - timedelta(days=i)
        heatmap_dates.append(d.strftime('%m/%d'))

    heatmap_query = '''
        SELECT uid, date(timestamp) as day
        FROM sign_ins
        WHERE timestamp >= date('now','-56 days')
        GROUP BY uid, day
    '''
    heatmap_rows = conn.execute(heatmap_query).fetchall()
    # 按 uid 分组
    heatmap_by_uid = {}
    for row in heatmap_rows:
        heatmap_by_uid.setdefault(row['uid'], set()).add(row['day'])

    heatmap_data = []
    for s in stats_list:
        uid = s['uid']
        name = s['name'] if s['name'] != '未绑定' else uid
        days_set = heatmap_by_uid.get(uid, set())
        day_flags = []
        for i in range(55, -1, -1):
            d = (today_date - timedelta(days=i)).strftime('%Y-%m-%d')
            day_flags.append(d in days_set)
        heatmap_data.append((uid, name, day_flags))

    # 柱状图数据：各成员近30天 Onsite 天数
    bar_labels = []
    bar_values = []
    for s in stats_list:
        uid = s['uid']
        count_30 = conn.execute(
            "SELECT COUNT(DISTINCT date(timestamp)) FROM sign_ins WHERE uid=? AND timestamp >= date('now','-30 days')",
            (uid,)
        ).fetchone()[0]
        bar_labels.append(s['name'] if s['name'] != '未绑定' else f"{s['uid']}")
        bar_values.append(count_30)

    conn.close()

    return render_template('stats.html', page='stats',
                           stats=stats_list, last_month_label=last_month_label,
                           bar_labels=bar_labels, bar_data=bar_values,
                           heatmap_data=heatmap_data, heatmap_dates=heatmap_dates,
                           uid_count=len(stats_list))


@dashboard_bp.route('/detail/<uid>')
@login_required
def detail(uid):
    """三周上下文：上周(完成情况) / 本周(计划+日报+AI) / 下周(待生效计划)。"""
    conn = get_db_connection()
    user = conn.execute("SELECT name FROM users WHERE uid=?", (uid,)).fetchone()
    name = user['name'] if user else "未绑定"

    now = datetime.now()
    today = now.date()
    current_monday = today - timedelta(days=today.weekday())
    last_mon = current_monday - timedelta(weeks=1)
    next_mon = current_monday + timedelta(weeks=1)
    last_key, this_key, next_key = (last_mon.strftime('%Y%W'),
                                    current_monday.strftime('%Y%W'),
                                    next_mon.strftime('%Y%W'))

    def rng(m):
        return f"{m.strftime('%Y-%m-%d')} ~ {(m + timedelta(days=6)).strftime('%Y-%m-%d')}"

    # 三周的 items + 旧 content 回退（各一次查询）
    items_by_key = {}
    for r in conn.execute(
        "SELECT week_key, item_order, text, status FROM weekly_plan_items "
        "WHERE uid=? AND week_key IN (?, ?, ?) ORDER BY week_key, item_order",
        (uid, last_key, this_key, next_key)
    ).fetchall():
        items_by_key.setdefault(r['week_key'], []).append({'text': r['text'], 'status': r['status']})
    legacy_by_key = {r['week_key']: r['content'] for r in conn.execute(
        "SELECT week_key, content FROM weekly_plans WHERE uid=? AND week_key IN (?, ?, ?)",
        (uid, last_key, this_key, next_key)).fetchall()}

    def build(m, key):
        it = items_by_key.get(key, [])
        if not it and legacy_by_key.get(key):
            # 旧 content（无 items）：按 '\n' 拆成条目，默认未完成 open
            it = [{'text': line, 'status': 'open'}
                  for line in legacy_by_key[key].split('\n') if line.strip()]
        return {'date_range': rng(m), 'plan_items': it, 'can_edit': False}

    last_week = build(last_mon, last_key)
    next_week = build(next_mon, next_key)
    this_week = build(current_monday, this_key)

    # 本周签到 / 日报 -> 7 天格子（也用于上周的只读展示）
    is_owner = (uid == uid_for_user(session['user']))
    editable_date_str = current_completion_date()
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

    def build_days(monday, editable):
        ws = monday.strftime('%Y-%m-%d')
        we = (monday + timedelta(weeks=1)).strftime('%Y-%m-%d')
        sign = set(r['day'] for r in conn.execute(
            "SELECT DISTINCT date(timestamp) as day FROM sign_ins WHERE uid=? AND timestamp >= ? AND timestamp < ?",
            (uid, ws, we)).fetchall())
        comp = {r['date']: {'review': r['review'], 'todo': r['todo']} for r in conn.execute(
            "SELECT date, review, todo FROM daily_completions WHERE uid=? AND date >= ? AND date < ?",
            (uid, ws, we)).fetchall()}
        out = []
        for d in range(7):
            dd = monday + timedelta(days=d)
            ds = dd.strftime('%Y-%m-%d')
            c = comp.get(ds)
            out.append({'weekday': weekdays[d], 'date_str': dd.strftime('%m/%d'), 'full_date': ds,
                        'present': ds in sign, 'future': dd > today,
                        'review': c['review'] if c else None, 'todo': c['todo'] if c else None,
                        'can_edit': bool(is_owner and editable and ds == editable)})
        return out

    this_week['days'] = build_days(current_monday, editable_date_str)
    last_week['days'] = build_days(last_mon, None)

    editable_week_key = compute_week_key(now)
    this_week['can_edit'] = bool(is_owner and editable_week_key and this_key == editable_week_key)
    next_week['can_edit'] = bool(is_owner and editable_week_key and next_key == editable_week_key)

    recent_checkins = [r['timestamp'] for r in conn.execute(
        "SELECT timestamp FROM sign_ins WHERE uid=? ORDER BY timestamp DESC LIMIT 30", (uid,)).fetchall()]

    conn.close()
    return render_template('detail.html', page='detail',
                           uid=uid, name=name, is_owner=is_owner,
                           last_week=last_week, this_week=this_week, next_week=next_week,
                           in_window=editable_week_key is not None,
                           recent_checkins=recent_checkins)


@dashboard_bp.route('/weekly_plan', methods=['POST'])
@login_required
def weekly_plan_save():
    """Web 保存一周计划：仅 owner、仅提交窗口内（保存到可提交那周）。"""
    uid = (request.form.get('uid') or '').strip()
    items = request.form.getlist('item')

    if not uid or uid != uid_for_user(session['user']):
        flash('只能编辑自己的周计划。')
        return redirect(url_for('dashboard.detail', uid=uid))

    editable_week_key = compute_week_key(datetime.now())
    if not editable_week_key:
        flash('当前不在提交窗口内（周六至周一18点）。')
        return redirect(url_for('dashboard.detail', uid=uid))

    try:
        save_weekly_plan(uid, editable_week_key, items)
    except ValueError as e:
        flash(str(e))
    else:
        flash('周计划已保存。')
    return redirect(url_for('dashboard.detail', uid=uid))


@dashboard_bp.route('/daily_completion', methods=['POST'])
@login_required
def daily_completion_save():
    """Web 编辑当天日报：仅 owner、仅当天（now-6h 归属日），多次编辑只保留最新。"""
    uid = (request.form.get('uid') or '').strip()
    review = request.form.get('review') or ''
    todo = request.form.get('todo') or ''

    if not uid or uid != uid_for_user(session['user']):
        flash('只能编辑自己的daily completion。')
        return redirect(url_for('dashboard.detail', uid=uid))

    try:
        save_daily_completion(uid, review, todo)
    except ValueError as e:
        flash(str(e))
    else:
        flash('Daily completion已保存。')
    return redirect(url_for('dashboard.detail', uid=uid))


@dashboard_bp.route('/monthly_summary')
@login_required
def monthly_summary():
    offset = request.args.get('month_offset', 0, type=int)
    if offset < 0:
        offset = 0
    start_date, end_date, month_key = compute_month_range(offset)
    date_range = f"{start_date.strftime('%Y-%m')}"
    summary_list = generate_monthly_summary(offset)
    return render_template('monthly_summary.html', page='monthly_summary',
                           summary_list=summary_list, date_range=date_range,
                           month_key=month_key, month_offset=offset)


@dashboard_bp.route('/monthly_summary/generate_summary/<uid>', methods=['POST'])
@login_required
def generate_summary(uid):
    offset = request.form.get('month_offset', 0, type=int)
    _, _, month_key = compute_month_range(offset)
    summary_list = generate_monthly_summary(offset)
    member = next((s for s in summary_list if s['uid'] == uid), None)
    if not member:
        flash('成员不存在或该月无活动')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    if member['summary']:
        flash('该成员的摘要已生成，无需重复生成')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    from llm import generate_daily_summary
    session_id = uuid.uuid4().hex
    result = generate_daily_summary(uid, member['name'], member['daily_completions_text'], session_id=session_id)
    if result:
        flash(f'{member["name"]} 的月度摘要已生成')
    else:
        flash('摘要生成失败，请检查 DeepSeek API 配置')
    return redirect(url_for('dashboard.monthly_summary', month_offset=offset))


@dashboard_bp.route('/monthly_summary/generate_suggestion/<uid>', methods=['POST'])
@login_required
def generate_suggestion(uid):
    offset = request.form.get('month_offset', 0, type=int)
    _, _, month_key = compute_month_range(offset)
    summary_list = generate_monthly_summary(offset)
    member = next((s for s in summary_list if s['uid'] == uid), None)
    if not member:
        flash('成员不存在或该月无活动')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    if member['suggestion']:
        flash('该成员的工作建议已生成，无需重复生成')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    from llm import generate_work_suggestion
    session_id = uuid.uuid4().hex
    result = generate_work_suggestion(uid, member['name'], member['daily_completions_text'], member['weekly_plans_text'], session_id=session_id)
    if result:
        flash(f'{member["name"]} 的工作建议已生成')
    else:
        flash('工作建议生成失败，请检查 DeepSeek API 配置')
    return redirect(url_for('dashboard.monthly_summary', month_offset=offset))