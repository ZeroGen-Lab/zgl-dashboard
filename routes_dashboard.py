from flask import Blueprint, render_template, request, redirect, url_for, flash
from datetime import datetime, timedelta
from db import get_db_connection
from auth import login_required
from helpers import compute_summary_week_range, generate_weekly_summary, compute_month_range, generate_monthly_summary

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
        SELECT s.uid, MAX(s.timestamp) as last_time, u.name
        FROM sign_ins s
        LEFT JOIN users u ON s.uid = u.uid
        WHERE s.timestamp >= date('now','-180 days')
        GROUP BY s.uid
        ORDER BY last_time DESC
    '''
    records = conn.execute(sql).fetchall()

    # 一次性预约轮播数据
    today_str = datetime.now().strftime('%Y-%m-%d')
    carousel_slots = conn.execute(
        "SELECT * FROM booking_slots WHERE slot_type='one_time' AND status='active' AND specific_date >= ? ORDER BY specific_date ASC LIMIT 5",
        (today_str,)
    ).fetchall()
    carousel_data = []
    for slot in carousel_slots:
        booked_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND instance_date=? AND status='active'",
            (slot['id'], slot['specific_date'])
        ).fetchone()[0]
        carousel_data.append(dict(slot=dict(slot), remaining=slot['capacity'] - booked_count))

    conn.close()
    return render_template('index.html', page='index', records=records, carousel_data=carousel_data)


@dashboard_bp.route('/bind', methods=['POST'])
@login_required
def bind():
    uid = request.form.get('uid')
    name = request.form.get('name')
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO users (uid, name) VALUES (?, ?)", (uid, name))
    conn.commit()
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
    offset = request.args.get('offset', 0, type=int)

    conn = get_db_connection()
    user = conn.execute("SELECT name FROM users WHERE uid=?", (uid,)).fetchone()
    name = user['name'] if user else "未绑定"

    # 计算当前周的周一
    today = datetime.now().date()
    current_monday = today - timedelta(days=today.weekday())
    # 当前天固定在第3行（w=2），所以视图起始从当前周的2周前开始
    view_monday = current_monday + timedelta(weeks=offset) - timedelta(weeks=2)
    view_end = view_monday + timedelta(weeks=4)

    date_range = f"{view_monday.strftime('%Y-%m-%d')} ~ {(view_end - timedelta(days=1)).strftime('%Y-%m-%d')}"

    # 查询签到数据
    sign_rows = conn.execute(
        "SELECT DISTINCT date(timestamp) as day FROM sign_ins WHERE uid=? AND timestamp >= ? AND timestamp < ?",
        (uid, view_monday.strftime('%Y-%m-%d'), view_end.strftime('%Y-%m-%d'))
    ).fetchall()
    sign_days = set(r['day'] for r in sign_rows)

    # 查询每日完成数据
    comp_rows = conn.execute(
        "SELECT date, content FROM daily_completions WHERE uid=? AND date >= ? AND date < ?",
        (uid, view_monday.strftime('%Y-%m-%d'), view_end.strftime('%Y-%m-%d'))
    ).fetchall()
    comp_map = {r['date']: r['content'] for r in comp_rows}

    # 查询每周计划数据
    plan_rows = conn.execute(
        "SELECT week_key, content FROM weekly_plans WHERE uid=?",
        (uid,)
    ).fetchall()
    plan_map = {r['week_key']: r['content'] for r in plan_rows}

    # 构建 4 周数据
    weeks = []
    for w in range(4):
        week_monday = view_monday + timedelta(weeks=w)
        week_key = week_monday.strftime('%Y%W')
        days = []
        for d in range(7):
            day_date = week_monday + timedelta(days=d)
            day_str = day_date.strftime('%Y-%m-%d')
            day_short = day_date.strftime('%m/%d')
            is_present = day_str in sign_days
            is_future = day_date > today
            completion = comp_map.get(day_str, None)
            days.append({
                'date_str': day_short,
                'full_date': day_str,
                'present': is_present,
                'future': is_future,
                'completion': completion
            })
        weeks.append({
            'days': days,
            'plan': plan_map.get(week_key, None)
        })

    # 查询最近30条刷卡明细（在关闭连接之前）
    recent_checkins = conn.execute(
        "SELECT timestamp FROM sign_ins WHERE uid=? ORDER BY timestamp DESC LIMIT 30",
        (uid,)
    ).fetchall()
    recent_checkins = [r['timestamp'] for r in recent_checkins]

    prev_offset = offset - 4
    next_offset = offset + 4

    conn.close()

    return render_template('detail.html', page='detail',
                           uid=uid, name=name, weeks=weeks,
                           date_range=date_range,
                           prev_offset=prev_offset, next_offset=next_offset,
                           recent_checkins=recent_checkins)


@dashboard_bp.route('/weekly_summary')
@login_required
def weekly_summary():
    offset = request.args.get('offset', 0, type=int)
    if offset < 0:
        offset = 0
    summary_monday, summary_sunday, _, _ = compute_summary_week_range(offset)
    date_range = f"{summary_monday.strftime('%Y-%m-%d')} ~ {summary_sunday.strftime('%Y-%m-%d')}"
    summary_list = generate_weekly_summary(offset)
    return render_template('weekly_summary.html', page='weekly_summary',
                           summary_list=summary_list, date_range=date_range,
                           offset=offset)


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
    result = generate_daily_summary(uid, member['name'], member['daily_completions_text'])
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
    result = generate_work_suggestion(uid, member['name'], member['daily_completions_text'], member['weekly_plans_text'])
    if result:
        flash(f'{member["name"]} 的工作建议已生成')
    else:
        flash('工作建议生成失败，请检查 DeepSeek API 配置')
    return redirect(url_for('dashboard.monthly_summary', month_offset=offset))