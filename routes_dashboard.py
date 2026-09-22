import sqlite3
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort
from datetime import datetime, timedelta
from db import get_db_connection
from auth import login_required, VALID_USERS, is_admin
from helpers import compute_month_range, generate_monthly_summary, is_instance_expired, compute_week_key, save_weekly_plan, save_daily_completion, current_completion_date

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@login_required
def index():
    conn = get_db_connection()

    # 清理超过三天的未知卡签到，已确认人员归属的历史记录始终保留。
    cutoff = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
    with conn:
        conn.execute('''
            DELETE FROM sign_ins
            WHERE loginname IS NULL AND timestamp < ?
              AND NOT EXISTS (
                  SELECT 1 FROM user_cards c WHERE c.uid = sign_ins.card_uid
              )
        ''', (cutoff,))

    # 人员按账号展示；即使删除全部卡片也保留该用户和历史签到。
    members = [dict(row) for row in conn.execute('''
        SELECT u.loginname, u.name, MAX(s.timestamp) AS last_time
        FROM users u LEFT JOIN sign_ins s ON s.loginname = u.loginname
        GROUP BY u.loginname, u.name
        ORDER BY last_time DESC, u.loginname
    ''').fetchall()]
    cards_by_user = {}
    # UID 用于展示，内部记录 ID 用于删除指定绑定。
    for row in conn.execute('SELECT id, uid, loginname FROM user_cards ORDER BY id'):
        cards_by_user.setdefault(row['loginname'], []).append(dict(row))
    for member in members:
        member['cards'] = cards_by_user.get(member['loginname'], [])
        member['can_manage_cards'] = member['loginname'] == session['user'] or is_admin()

    # 这里只读展示，不因删除卡片关系就清理其历史签到记录。
    unbound_cards = conn.execute('''
        SELECT s.card_uid AS uid, MAX(s.timestamp) AS last_time
        FROM sign_ins s
        WHERE s.card_uid IS NOT NULL AND s.card_uid != ''
          AND s.timestamp >= date('now','-180 days')
          AND NOT EXISTS (SELECT 1 FROM user_cards c WHERE c.uid = s.card_uid)
        GROUP BY s.card_uid ORDER BY last_time DESC
    ''').fetchall()

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
    return render_template('index.html', page='index', members=members,
                           unbound_cards=unbound_cards,
                           carousel_data=carousel_data)


@dashboard_bp.route('/bind', methods=['POST'])
@login_required
def bind():
    """将未绑定的卡绑定到当前登录账号，支持一人多卡。"""
    uid = (request.form.get('uid') or '').strip()
    loginname = session['user']

    if not uid:
        flash('缺少卡片 UID。')
        return redirect(url_for('dashboard.index'))
    if loginname not in VALID_USERS:
        flash('当前登录账号无效，请重新登录。')
        return redirect(url_for('dashboard.index'))

    conn = get_db_connection()
    try:
        # 账号创建和卡片占用检查处于同一写事务，防止并发抢绑。
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT id FROM user_cards WHERE uid=?', (uid,)).fetchone():
            flash('该卡片已经绑定，请先删除原卡片关系。')
            return redirect(url_for('dashboard.index'))
        user = conn.execute(
            'SELECT loginname FROM users WHERE loginname=?', (loginname,)).fetchone()
        if not user:
            conn.execute('INSERT INTO users(loginname, name) VALUES (?, ?)', (loginname, loginname))
        # 已有用户沿用原资料，不因新增卡片覆盖姓名。
        conn.execute('INSERT INTO user_cards(uid, loginname) VALUES (?, ?)', (uid, loginname))
        conn.commit()
        flash('绑定成功。')
    except sqlite3.IntegrityError:
        conn.rollback()
        flash('绑定失败：该卡片可能已被绑定，请刷新后重试。')
    finally:
        conn.close()
    return redirect(url_for('dashboard.index'))


@dashboard_bp.route('/delete_card', methods=['POST'])
@login_required
def delete_card():
    """只删除指定卡片关系，账号及历史数据保留。"""
    binding_id = request.form.get('binding_id', type=int)
    if binding_id is None or binding_id <= 0:
        flash('缺少有效的卡片绑定编号。')
        return redirect(url_for('dashboard.index'))

    conn = get_db_connection()
    try:
        card = conn.execute('SELECT loginname FROM user_cards WHERE id=?', (binding_id,)).fetchone()
        if card and card['loginname'] != session['user'] and not is_admin():
            abort(403)
        with conn:
            result = conn.execute('DELETE FROM user_cards WHERE id=?', (binding_id,))
        flash('卡片已删除，账号和历史数据已保留。' if result.rowcount else '该卡片已删除或不存在。')
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

    # 1次查询：所有用户在180天内的 stats（JOIN users 表，GROUP BY loginname）
    stats_query = '''
        SELECT
            s.loginname,
            COALESCE(NULLIF(u.name, ''), u.loginname) as name,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-15 days') THEN date(s.timestamp) END) as d15,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-60 days') THEN date(s.timestamp) END) as d60,
            COUNT(DISTINCT CASE WHEN s.timestamp >= date('now','-180 days') THEN date(s.timestamp) END) as d180,
            COUNT(DISTINCT CASE WHEN strftime('%Y-%m',s.timestamp) = strftime('%Y-%m','now','start of month','-1 month') THEN date(s.timestamp) END) as last_month
        FROM sign_ins s
        JOIN users u ON s.loginname = u.loginname
        WHERE s.timestamp >= date('now','-180 days')
        GROUP BY s.loginname
        ORDER BY d15 DESC
    '''
    stats_list = [dict(row) for row in conn.execute(stats_query).fetchall()]

    # 1次查询：全员56天热力图数据（GROUP BY loginname）
    heatmap_dates = []
    today_date = datetime.now().date()
    for i in range(55, -1, -1):
        d = today_date - timedelta(days=i)
        heatmap_dates.append(d.strftime('%m/%d'))

    heatmap_query = '''
        SELECT loginname, date(timestamp) as day
        FROM sign_ins
        WHERE loginname IS NOT NULL AND timestamp >= date('now','-56 days')
        GROUP BY loginname, day
    '''
    heatmap_rows = conn.execute(heatmap_query).fetchall()
    # 按 loginname 分组
    heatmap_by_loginname = {}
    for row in heatmap_rows:
        heatmap_by_loginname.setdefault(row['loginname'], set()).add(row['day'])

    heatmap_data = []
    for s in stats_list:
        loginname = s['loginname']
        name = s['name']
        days_set = heatmap_by_loginname.get(loginname, set())
        day_flags = []
        for i in range(55, -1, -1):
            d = (today_date - timedelta(days=i)).strftime('%Y-%m-%d')
            day_flags.append(d in days_set)
        heatmap_data.append((loginname, name, day_flags))

    # 柱状图数据：各成员近30天 Onsite 天数
    bar_labels = []
    bar_values = []
    for s in stats_list:
        loginname = s['loginname']
        count_30 = conn.execute(
            "SELECT COUNT(DISTINCT date(timestamp)) FROM sign_ins WHERE loginname=? AND timestamp >= date('now','-30 days')",
            (loginname,)
        ).fetchone()[0]
        bar_labels.append(s['name'])
        bar_values.append(count_30)

    conn.close()

    return render_template('stats.html', page='stats',
                           stats=stats_list, last_month_label=last_month_label,
                           bar_labels=bar_labels, bar_data=bar_values,
                           heatmap_data=heatmap_data, heatmap_dates=heatmap_dates,
                           member_count=len(stats_list))


@dashboard_bp.route('/detail/<loginname>')
@login_required
def detail(loginname):
    """三周上下文：上周(完成情况) / 本周(计划+日报+AI) / 下周(待生效计划)。"""
    conn = get_db_connection()
    user = conn.execute("SELECT name FROM users WHERE loginname=?", (loginname,)).fetchone()
    if not user:
        conn.close()
        abort(404)
    name = user['name'] or loginname

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
        "WHERE loginname=? AND week_key IN (?, ?, ?) ORDER BY week_key, item_order",
        (loginname, last_key, this_key, next_key)
    ).fetchall():
        items_by_key.setdefault(r['week_key'], []).append({'text': r['text'], 'status': r['status']})
    legacy_by_key = {r['week_key']: r['content'] for r in conn.execute(
        "SELECT week_key, content FROM weekly_plans WHERE loginname=? AND week_key IN (?, ?, ?)",
        (loginname, last_key, this_key, next_key)).fetchall()}

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
    is_owner = (loginname == session['user'])
    editable_date_str = current_completion_date()
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

    def build_days(monday, editable):
        ws = monday.strftime('%Y-%m-%d')
        we = (monday + timedelta(weeks=1)).strftime('%Y-%m-%d')
        sign = set(r['day'] for r in conn.execute(
            "SELECT DISTINCT date(timestamp) as day FROM sign_ins WHERE loginname=? AND timestamp >= ? AND timestamp < ?",
            (loginname, ws, we)).fetchall())
        comp = {r['date']: {'review': r['review'], 'todo': r['todo']} for r in conn.execute(
            "SELECT date, review, todo FROM daily_completions WHERE loginname=? AND date >= ? AND date < ?",
            (loginname, ws, we)).fetchall()}
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
        "SELECT timestamp FROM sign_ins WHERE loginname=? ORDER BY timestamp DESC LIMIT 30", (loginname,)).fetchall()]

    conn.close()
    return render_template('detail.html', page='detail',
                           loginname=loginname, name=name, is_owner=is_owner,
                           last_week=last_week, this_week=this_week, next_week=next_week,
                           in_window=editable_week_key is not None,
                           recent_checkins=recent_checkins)


@dashboard_bp.route('/weekly_plan', methods=['POST'])
@login_required
def weekly_plan_save():
    """Web 保存一周计划：仅 owner、仅提交窗口内（保存到可提交那周）。"""
    loginname = (request.form.get('loginname') or '').strip()
    items = request.form.getlist('item')

    if not loginname or loginname != session['user']:
        flash('只能编辑自己的周计划。')
        return redirect(url_for('dashboard.index'))

    editable_week_key = compute_week_key(datetime.now())
    if not editable_week_key:
        flash('当前不在提交窗口内（周六至周一18点）。')
        return redirect(url_for('dashboard.detail', loginname=loginname))

    try:
        save_weekly_plan(loginname, editable_week_key, items)
    except ValueError as e:
        flash(str(e))
    else:
        flash('周计划已保存。')
    return redirect(url_for('dashboard.detail', loginname=loginname))


@dashboard_bp.route('/daily_completion', methods=['POST'])
@login_required
def daily_completion_save():
    """Web 编辑当天日报：仅 owner、仅当天（now-6h 归属日），多次编辑只保留最新。"""
    loginname = (request.form.get('loginname') or '').strip()
    review = request.form.get('review') or ''
    todo = request.form.get('todo') or ''

    if not loginname or loginname != session['user']:
        flash('只能编辑自己的daily completion。')
        return redirect(url_for('dashboard.index'))

    try:
        save_daily_completion(loginname, review, todo)
    except ValueError as e:
        flash(str(e))
    else:
        flash('Daily completion已保存。')
    return redirect(url_for('dashboard.detail', loginname=loginname))


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


@dashboard_bp.route('/monthly_summary/generate_summary/<loginname>', methods=['POST'])
@login_required
def generate_summary(loginname):
    offset = max(0, request.form.get('month_offset', 0, type=int))
    _, _, month_key = compute_month_range(offset)
    summary_list = generate_monthly_summary(offset)
    member = next((s for s in summary_list if s['loginname'] == loginname), None)
    if not member:
        flash('成员不存在或该月无活动')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    if member['summary']:
        flash('该成员的摘要已生成，无需重复生成')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    from llm import generate_daily_summary
    session_id = uuid.uuid4().hex
    result = generate_daily_summary(loginname, member['name'], member['daily_completions_text'], session_id=session_id, month_key=month_key)
    if result:
        flash(f'{member["name"]} 的月度摘要已生成')
    else:
        flash('摘要生成失败，请检查 DeepSeek API 配置')
    return redirect(url_for('dashboard.monthly_summary', month_offset=offset))


@dashboard_bp.route('/monthly_summary/generate_suggestion/<loginname>', methods=['POST'])
@login_required
def generate_suggestion(loginname):
    offset = max(0, request.form.get('month_offset', 0, type=int))
    _, _, month_key = compute_month_range(offset)
    summary_list = generate_monthly_summary(offset)
    member = next((s for s in summary_list if s['loginname'] == loginname), None)
    if not member:
        flash('成员不存在或该月无活动')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    if member['suggestion']:
        flash('该成员的工作建议已生成，无需重复生成')
        return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
    from llm import generate_work_suggestion
    session_id = uuid.uuid4().hex
    result = generate_work_suggestion(loginname, member['name'], member['daily_completions_text'], member['weekly_plans_text'], session_id=session_id, month_key=month_key)
    if result:
        flash(f'{member["name"]} 的工作建议已生成')
    else:
        flash('工作建议生成失败，请检查 DeepSeek API 配置')
    return redirect(url_for('dashboard.monthly_summary', month_offset=offset))
