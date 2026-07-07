"""ZGantt 项目管理 Blueprint：项目甘特图 + 员工月度考勤。

- 项目（在研 active / 结项 closed，单向）；管理员建项，启动日=今天且不可变；
  按 loginname 加成员，成员可设为离职(departed)但永不删除。
- 工作组（无进度概念）→ 工作项（owner=创建者固定；end_date NULL=进行中，非空=已完成）。
  只有 owner 本人(在职)或管理员可完成工作项，完成时必填一句话总结（悬停可见）。
- 考勤按「项目 + 当前登录成员本人」维度，按自然月填报(半天0.5/全天1.0/加班1.5)，
  汇总当月补贴天数。
- 所有项目对所有人只读可见；只有在职成员可编辑所属项目；结项后全只读。
"""
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, admin_required, is_admin, VALID_USERS
from db import get_db_connection
from helpers import compute_month_range

zgantt_bp = Blueprint('zgantt', __name__, url_prefix='/zgantt')

ATT_WEIGHTS = {'half': 0.5, 'full': 1.0, 'overtime': 1.5}
ATT_TYPES = ('half', 'full', 'overtime')
STATUS_FILTERS = ('all', 'active', 'closed')
WEEKDAY_CN = ('一', '二', '三', '四', '五', '六', '日')   # 下标 0=周一 .. 6=周日


# ---------- 局部辅助 ----------

def is_active_member(conn, project_id, loginname):
    """loginname 是否为该项目的在职成员（离职=False）。"""
    row = conn.execute(
        "SELECT 1 FROM zgantt_members WHERE zgantt_id=? AND loginname=? AND status='active'",
        (project_id, loginname)).fetchone()
    return row is not None


def assert_project_open(conn, project_id):
    """返回 active 项目行；结项/不存在返回 None（所有写操作需项目在研）。"""
    return conn.execute(
        "SELECT * FROM zgantts WHERE id=? AND status='active'", (project_id,)).fetchone()


def display_name(conn, loginname):
    """loginname -> 展示名；users 表查不到则回退 loginname 本身。"""
    row = conn.execute("SELECT name FROM users WHERE loginname=?", (loginname,)).fetchone()
    return row['name'] if row and row['name'] else loginname


def attendance_month_range(months_back):
    """months_back: 0=当月, 1=上月... 返回 (start_date, end_date_exclusive, month_key)。

    注：helpers.compute_month_range(x) 中 x=0 表示「上月」，故这里传 x=months_back-1。
    """
    return compute_month_range(months_back - 1)


def day_fillable(d, today):
    """某日是否可填考勤（GET 展示与 POST 服务端复用同一逻辑）。纯按自然月口径：

    - 当月：1 号 .. 今天都可填（即 X 号可填本月 1..X 日）；
    - 今天是 1~3 号时，额外允许上一自然月的所有天；
    - 其余过往/未来日期只读。
    """
    if d > today:
        return False
    if d.year == today.year and d.month == today.month:
        return True
    if today.day in (1, 2, 3):
        prev = today.replace(day=1) - timedelta(days=1)
        if d.year == prev.year and d.month == prev.month:
            return True
    return False


def build_gantt_context(conn, project, current_user, admin_flag):
    """构甘特图渲染数据：天数轴(项目开始 → 今天+15) + 两级排序的 group/item。"""
    start = datetime.strptime(project['start_date'], '%Y-%m-%d').date()
    today = datetime.now().date()
    end = today + timedelta(days=15)
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    date_index = {dd.isoformat(): i for i, dd in enumerate(days)}
    today_iso = today.isoformat()

    groups = []
    for g in conn.execute(
            "SELECT * FROM work_groups WHERE zgantt_id=? ORDER BY created_at, id",
            (project['id'],)).fetchall():
        items = []
        for it in conn.execute(
                "SELECT * FROM work_items WHERE group_id=? ORDER BY created_at, id",
                (g['id'],)).fetchall():
            s_col = date_index.get(it['start_date'])
            is_done = it['end_date'] is not None
            e_col = date_index.get(it['end_date'] if is_done else today_iso)
            if s_col is None or e_col is None:
                col_start, span = 2, 1
            else:
                col_start = s_col + 2          # grid 第1列是标签，天数从第2列开始
                span = max(1, e_col - s_col + 1)
            # 悬停只展示完成总结（标题/owner 已在格内展示，无需重复）
            tip = it['summary'] if (is_done and it['summary']) else ''
            can_complete = (not is_done) and (
                (it['owner'] == current_user and is_active_member(conn, project['id'], current_user))
                or admin_flag)
            items.append({
                'id': it['id'], 'title': it['title'],
                'owner_name': display_name(conn, it['owner']),
                'is_done': is_done, 'summary': it['summary'],
                'col_start': col_start, 'span': span,
                'cls': 'done' if is_done else 'progress',
                'tooltip': tip, 'can_complete': can_complete,
            })
        groups.append({'id': g['id'], 'name': g['name'], 'work_items': items})

    header = []
    for i, dd in enumerate(days):
        if i == 0 or dd.day in (1, 11, 21):          # 首日 / 每月 1 号 / 11 号 / 21 号 -> mm/dd
            lab = f"{dd.month}/{dd.day}"
        else:
            lab = str(dd.day)                     # 其余只显示 dd
        header.append({'iso': dd.isoformat(), 'dom': dd.day, 'label': lab,
                       'weekend': dd.weekday() >= 5, 'today': dd == today})
    return {'days': header, 'groups': groups, 'n_days': len(days)}


def build_attendance_grid(conn, project, members, months_back):
    """构「全员」考勤网格：列=当月每一天，行=每个成员（行尾带本人当月合计）。

    表头稀疏打标签（周一 / 1 号），避免每日文字密集重叠；具体日期靠单元格 title 悬停查看。
    编辑权由模板按 editable_loginname + day.fillable 判定：仅本人可编辑自己行里「可填」的日期。
    """
    today = datetime.now().date()
    m_start, m_end, m_key = attendance_month_range(months_back)
    rows = conn.execute(
        "SELECT loginname, date, att_type FROM zgantt_attendance "
        "WHERE zgantt_id=? AND date>=? AND date<?",
        (project['id'], m_start.isoformat(), m_end.isoformat())).fetchall()
    by_user = {}
    for r in rows:
        by_user.setdefault(r['loginname'], {})[r['date']] = r['att_type']

    days = []
    d = m_start
    while d < m_end:
        days.append({
            'iso': d.isoformat(), 'dom': d.day, 'weekday': d.weekday(),
            'weekday_cn': WEEKDAY_CN[d.weekday()],
            'anchor': (d.day % 2 == 1),    
            'weekend': d.weekday() >= 5,
            'today': d == today,
            'fillable': day_fillable(d, today),
        })
        d += timedelta(days=1)

    member_rows = []
    for mb in members:
        amap = by_user.get(mb['loginname'], {})
        member_rows.append({
            'loginname': mb['loginname'], 'display': mb['display'],
            'status': mb['status'], 'att_map': amap,
            'total': sum(ATT_WEIGHTS[t] for t in amap.values()),
        })
    return {'days': days, 'rows': member_rows, 'm_key': m_key, 'n_days': len(days)}


# ---------- 页面 ----------

@zgantt_bp.route('/')
@login_required
def project_page():
    user = session['user']
    admin = is_admin()
    status = request.args.get('status', 'all')
    if status not in STATUS_FILTERS:
        status = 'all'
    m = request.args.get('m', 0, type=int)
    if m < 0:
        m = 0

    conn = get_db_connection()
    try:
        # 合法用户清单（来自 .users.txt -> VALID_USERS），供「新建/添加成员」多选使用
        # label：已绑定 name 显示「name（loginname）」，未绑定只显示 loginname（避免重复）
        valid_users = []
        for ln in sorted(VALID_USERS, key=lambda x: x):
            disp = display_name(conn, ln)
            valid_users.append({'loginname': ln, 'display': disp,
                                'label': f"{disp}（{ln}）" if disp != ln else ln})
        if status == 'all':
            drop = conn.execute(
                "SELECT id, name, status FROM zgantts ORDER BY created_at DESC, id DESC").fetchall()
        else:
            drop = conn.execute(
                "SELECT id, name, status FROM zgantts WHERE status=? ORDER BY created_at DESC, id DESC",
                (status,)).fetchall()

        sel_id = request.args.get('project_id', type=int)
        if sel_id is None and drop:
            sel_id = drop[0]['id']
        if drop and not any(p['id'] == sel_id for p in drop):
            sel_id = drop[0]['id']      # 选中项不在当前过滤结果里，回落到第一个

        project = None
        members = []
        gantt_ctx = {'days': [], 'groups': [], 'n_days': 0}
        att_ctx = {'days': [], 'rows': [], 'm_key': '', 'n_days': 0}
        can_edit = False
        project_open = False
        editable_loginname = None

        if sel_id is not None:
            project = conn.execute("SELECT * FROM zgantts WHERE id=?", (sel_id,)).fetchone()
        if project:
            project_open = (project['status'] == 'active')
            can_edit = project_open and is_active_member(conn, project['id'], user)
            editable_loginname = user if can_edit else None
            members = [dict(r) for r in conn.execute(
                "SELECT pm.*, u.name FROM zgantt_members pm "
                "LEFT JOIN users u ON pm.loginname=u.loginname "
                "WHERE pm.zgantt_id=? ORDER BY (pm.status='active') DESC, pm.created_at",
                (project['id'],)).fetchall()]
            for mb in members:
                mb['display'] = mb['name'] or mb['loginname']
            gantt_ctx = build_gantt_context(conn, project, user, admin)
            att_ctx = build_attendance_grid(conn, project, members, m)
    finally:
        conn.close()

    return render_template('zgantt.html', page='zgantt',
                           projects=drop, status=status, m=m,
                           project=project, members=members,
                           member_by_login={mb['loginname']: mb for mb in members},
                           gantt=gantt_ctx, att=att_ctx,
                           can_edit=can_edit, project_open=project_open,
                           is_admin=admin, current_user=user,
                           valid_users=valid_users,
                           editable_loginname=editable_loginname)


# ---------- 写操作：项目/成员（管理员） ----------

@zgantt_bp.route('/create', methods=['POST'])
@login_required
@admin_required
def create_project():
    name = (request.form.get('name') or '').strip()
    raw_members = request.form.getlist('members')
    if not name:
        flash('项目名称不能为空')
        return redirect(url_for('zgantt.project_page'))
    valid = [ln for ln in raw_members if ln in VALID_USERS]
    skipped = [ln for ln in raw_members if ln not in VALID_USERS]

    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO zgantts (name, status, start_date, created_by) VALUES (?, 'active', ?, ?)",
            (name, datetime.now().strftime('%Y-%m-%d'), session['user']))
        pid = cur.lastrowid
        for ln in valid:
            conn.execute(
                "INSERT OR IGNORE INTO zgantt_members (zgantt_id, loginname, status) VALUES (?, ?, 'active')",
                (pid, ln))
        conn.commit()
    finally:
        conn.close()

    msg = f'项目「{name}」已创建，启动日为今天'
    if skipped:
        msg += f'；未识别的成员已忽略：{", ".join(skipped)}'
    flash(msg)
    return redirect(url_for('zgantt.project_page', project_id=pid))


@zgantt_bp.route('/members/sync', methods=['POST'])
@login_required
@admin_required
def sync_members():
    """一次性同步项目成员（在「项目成员调整」面板里勾选）：
    勾选=加入/复活，取消勾选(当前在职)=离职。离职成员勾选则复活。
    """
    pid = request.form.get('project_id', type=int)
    selected = set(request.form.getlist('members'))
    conn = get_db_connection()
    try:
        if not assert_project_open(conn, pid):
            flash('项目不存在或已结项')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        cur = {r['loginname']: r['status'] for r in conn.execute(
            "SELECT loginname, status FROM zgantt_members WHERE zgantt_id=?", (pid,)).fetchall()}
        for ln in VALID_USERS:
            want = ln in selected
            st = cur.get(ln)
            if want and st is None:                         # 非成员 -> 加入
                conn.execute(
                    "INSERT INTO zgantt_members (zgantt_id, loginname, status) VALUES (?, ?, 'active')",
                    (pid, ln))
            elif want and st == 'departed':                 # 离职 -> 复活
                conn.execute(
                    "UPDATE zgantt_members SET status='active', departed_at=NULL "
                    "WHERE zgantt_id=? AND loginname=?", (pid, ln))
            elif not want and st == 'active':               # 在职 -> 离职
                conn.execute(
                    "UPDATE zgantt_members SET status='departed', departed_at=datetime('now','localtime') "
                    "WHERE zgantt_id=? AND loginname=?", (pid, ln))
        conn.commit()
    finally:
        conn.close()
    flash('成员已更新')
    return redirect(url_for('zgantt.project_page', project_id=pid))


@zgantt_bp.route('/close', methods=['POST'])
@login_required
@admin_required
def close_project():
    pid = request.form.get('project_id', type=int)
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE zgantts SET status='closed', closed_at=datetime('now','localtime') "
            "WHERE id=? AND status='active'", (pid,))
        conn.commit()
    finally:
        conn.close()
    flash('项目已结项')
    return redirect(url_for('zgantt.project_page', project_id=pid))


# ---------- 写操作：工作组 / 工作项（在职成员） ----------

@zgantt_bp.route('/work_group/add', methods=['POST'])
@login_required
def add_work_group():
    pid = request.form.get('project_id', type=int)
    name = (request.form.get('name') or '').strip()
    user = session['user']
    conn = get_db_connection()
    try:
        if not assert_project_open(conn, pid):
            flash('项目不存在或已结项')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not is_active_member(conn, pid, user):
            flash('只有项目成员可以操作')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not name:
            flash('工作组名称不能为空')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        conn.execute("INSERT INTO work_groups (zgantt_id, name, created_by) VALUES (?, ?, ?)",
                     (pid, name, user))
        conn.commit()
    finally:
        conn.close()
    flash(f'工作组「{name}」已添加')
    return redirect(url_for('zgantt.project_page', project_id=pid))


@zgantt_bp.route('/work_item/add', methods=['POST'])
@login_required
def add_work_item():
    pid = request.form.get('project_id', type=int)
    gid = request.form.get('group_id', type=int)
    title = (request.form.get('title') or '').strip()
    user = session['user']
    conn = get_db_connection()
    try:
        if not assert_project_open(conn, pid):
            flash('项目不存在或已结项')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not is_active_member(conn, pid, user):
            flash('只有项目成员可以操作')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not conn.execute("SELECT 1 FROM work_groups WHERE id=? AND zgantt_id=?", (gid, pid)).fetchone():
            flash('工作组不存在')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not title:
            flash('工作项标题不能为空')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        conn.execute(
            "INSERT INTO work_items (group_id, zgantt_id, title, owner, start_date) VALUES (?, ?, ?, ?, ?)",
            (gid, pid, title, user, datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
    finally:
        conn.close()
    flash(f'工作项「{title}」已添加')
    return redirect(url_for('zgantt.project_page', project_id=pid))


@zgantt_bp.route('/work_item/complete/<int:item_id>', methods=['POST'])
@login_required
def complete_work_item(item_id):
    user = session['user']
    conn = get_db_connection()
    pid = None
    try:
        it = conn.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            flash('工作项不存在')
            return redirect(url_for('zgantt.project_page'))
        pid = it['zgantt_id']
        if it['end_date'] is not None:
            flash('该工作项已完成')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        if not assert_project_open(conn, pid):
            flash('项目已结项，不可修改')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        owner_ok = (it['owner'] == user and is_active_member(conn, pid, user))
        if not (owner_ok or is_admin()):
            flash('只有 owner 本人或管理员可以完成该工作项')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        summary = (request.form.get('summary') or '').strip()
        if not summary:
            flash('请填写完成总结')
            return redirect(url_for('zgantt.project_page', project_id=pid))
        conn.execute(
            "UPDATE work_items SET end_date=?, summary=? WHERE id=? AND end_date IS NULL",
            (datetime.now().strftime('%Y-%m-%d'), summary, item_id))
        conn.commit()
        flash('工作项已标记完成')
    finally:
        conn.close()
    return redirect(url_for('zgantt.project_page', project_id=pid))


# ---------- 写操作：考勤（在职成员，本人行） ----------

@zgantt_bp.route('/attendance/set', methods=['POST'])
@login_required
def set_attendance():
    pid = request.form.get('project_id', type=int)
    date_str = (request.form.get('date') or '').strip()
    att_type = (request.form.get('att_type') or '').strip()
    m = request.form.get('m', 0, type=int)
    if m < 0:
        m = 0
    user = session['user']
    conn = get_db_connection()
    try:
        proj = assert_project_open(conn, pid)
        if not proj:
            flash('项目不存在或已结项')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        if not is_active_member(conn, pid, user):
            flash('只有项目成员可以填报考勤')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('日期无效')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        if not day_fillable(d, datetime.now().date()):
            flash('该日期不在可填报范围内')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        if att_type not in ATT_TYPES:
            flash('出勤类型无效')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        # 不可变：已填报的日期不可再改
        if conn.execute(
                "SELECT 1 FROM zgantt_attendance WHERE zgantt_id=? AND loginname=? AND date=?",
                (pid, user, date_str)).fetchone():
            flash('该日期已填报，不可修改')
            return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
        conn.execute(
            "INSERT INTO zgantt_attendance (zgantt_id, loginname, date, att_type) "
            "VALUES (?, ?, ?, ?)", (pid, user, date_str, att_type))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('zgantt.project_page', project_id=pid, m=m))
