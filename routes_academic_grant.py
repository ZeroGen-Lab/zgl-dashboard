"""学术交流经费管理（AGrant）

- 每位研究生入学由管理员授予一笔经费（仅限已完成 loginname-name 绑定的用户），无需审批即可使用：本人申报使用（输入额度+说明）直接扣减余额。
- 成员之间可互相转赠额度，手续费 10%：received = (amount*9+5)//10（实收 90% 四舍五入到整数）；
  转出额与调整扣减额均不得超过当前余额（两者不支持透支，透支下限仅适用于申报使用）。
- 成员可设置毕业（status=graduated），仅作用于本页面，禁止授予/调整/申报/转出/转入，历史记录保留可见，可由管理员撤销（复活）。
- 金额一律整数元；余额最低为-500（应用层校验 + DB CHECK 双保险）。
- 流水只增不改不删，对账恒等式：每账户 SUM(records.amount) == accounts.balance。
"""
import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, admin_required, is_admin, VALID_USERS
from db import get_db_connection

academic_grant_bp = Blueprint('academic_grant', __name__, url_prefix='/academic_grant')

MAX_AMOUNT = 10_000          # 单次操作金额上限（元）
OVERDRAFT_LIMIT = 500        # 透支下限（元）：申报使用后余额最低可至 -500（转出/调整不得超当前余额）
MAX_NOTE_LEN = 50            # 说明字段长度上限（字）
SCOPES = ('active', 'all')   # 页面展示范围：活跃同学(默认) / 所有同学(含毕业)


# ---------- 局部辅助 ----------

def display_name(conn, loginname):
    """loginname -> 展示名；users 表查不到则回退 loginname 本身。"""
    row = conn.execute("SELECT name FROM users WHERE loginname=?", (loginname,)).fetchone()
    return f"{row['name']} ({loginname})" if row and row['name'] else loginname


def load_user_names(conn):
    """一次取全量 users 表，返回 {loginname: name or ''}，供批量组装展示名与「已绑定」判定。"""
    return {r['loginname']: (r['name'] or '')
            for r in conn.execute("SELECT loginname, name FROM users").fetchall()}


def get_account(conn, loginname):
    """返回该同学的经费账户行；未开户返回 None。"""
    return conn.execute(
        "SELECT * FROM academic_grant_accounts WHERE loginname=?", (loginname,)).fetchone()


def add_record(conn, loginname, record_type, amount, note='', related_loginname=None):
    """追加一条经费流水（只增不改不删），created_by=当前登录用户。"""
    conn.execute(
        "INSERT INTO academic_grant_records "
        "(loginname, record_type, amount, note, related_loginname, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (loginname, record_type, amount, note, related_loginname, session['user']))


def normalize_scope(value):
    """scope 白名单归一化：非法/缺失一律回退 'active'（GET 查询串与 POST 表单共用）。"""
    return value if value in SCOPES else 'active'


def form_scope():
    """POST 表单隐藏字段透传的 scope，归一化后用于回跳当前视图。"""
    return normalize_scope(request.form.get('scope'))


# ---------- 页面 ----------

@academic_grant_bp.route('/')
@login_required
def grant_page():
    user = session['user']
    admin = is_admin()
    scope = normalize_scope(request.args.get('scope'))

    conn = get_db_connection()
    try:
        global_names = load_user_names(conn)

        # 排序：当前用户最前（自查余额最高频）→ 活跃同学（按loginname）→ 毕业（按loginname）
        accounts = [dict(r) for r in conn.execute(
            "SELECT * FROM academic_grant_accounts "
            "ORDER BY (loginname=?) DESC, (status='active') DESC, loginname",
            (user,)).fetchall()]
        for acc in accounts:
            acc['display'] = global_names.get(acc['loginname']) or acc['loginname']
            acc['records'] = []
        all_accounts_lns = {a['loginname'] for a in accounts}

        # scope 过滤放在账户组装之后：all_account_lns 等基于全量，这里只裁剪可见卡片
        if scope == 'active':
            accounts = [a for a in accounts if a['status'] == 'active']

        # 流水只取当前展示的账户，一次倒序（新的在前）分组挂载；相关人展示名预先解析。IN 占位符只拼结构，值走参数绑定。
        acc_by_ln = {a['loginname']: a for a in accounts}
        if acc_by_ln:
            marks = ','.join('?' * len(acc_by_ln))
            for r in conn.execute(
                    f"SELECT * FROM academic_grant_records WHERE loginname IN ({marks}) "
                    "ORDER BY created_at DESC, id DESC", tuple(acc_by_ln)).fetchall():
                rec = dict(r)
                rec['related_display'] = ((global_names.get(rec['related_loginname']) or rec['related_loginname'])
                                          if rec['related_loginname'] else '')
                acc_by_ln[rec['loginname']]['records'].append(rec)

        # 管理员授予候选：已完成姓名绑定（name 非空且 != loginname）的合法用户；未开户者排前
        grant_candidates = []
        if admin:
            for ln in sorted(VALID_USERS):
                nm = global_names.get(ln)
                if not nm or nm == ln:
                    continue
                grant_candidates.append({
                    'loginname': ln, 'display': nm, 'has_account': ln in all_accounts_lns,
                    'label': f"{nm}（{ln}）" + (' · 已开户' if ln in all_accounts_lns else '')})
            grant_candidates.sort(key=lambda c: (c['has_account'], c['display']))

        # 转赠收款候选：活跃且非本人的账户（毕业冻结不能收款）
        recipients = sorted(
            ({'loginname': a['loginname'], 'display': a['display']}
             for a in accounts if a['status'] == 'active' and a['loginname'] != user),
            key=lambda c: c['display'])
    finally:
        conn.close()

    return render_template('academic_grant.html', page='academic_grant',
                           scope=scope, accounts=accounts,
                           grant_candidates=grant_candidates, recipients=recipients,
                           overdraft_limit=OVERDRAFT_LIMIT,
                           current_user=user, is_admin=admin)


# ---------- 写操作（全部 PRG：flash + redirect 回当前视图） ----------

@academic_grant_bp.route('/grant', methods=['POST'])
@login_required
@admin_required
def grant():
    """管理员授予（仅正向变动，输入正数）；首次授予即开户。扣减统一走 /adjust/<loginname>。"""
    scope = form_scope()
    loginname = (request.form.get('loginname') or '').strip()
    amount = request.form.get('amount', type=int)
    note = (request.form.get('note') or '').strip()

    if amount is None or amount <= 0:
        flash('金额必须是正整数；扣减请在成员卡片上使用「调整」。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if amount > MAX_AMOUNT:
        flash(f'金额超出允许范围（单次最多 {MAX_AMOUNT:,} 元）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if loginname not in VALID_USERS:
        flash('该用户不存在。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if not note:
        flash('请填写有效的说明（如：入学授予 / 团队贡献追加）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if len(note) > MAX_NOTE_LEN:
        flash(f'说明不能超过 {MAX_NOTE_LEN} 字。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))

    conn = get_db_connection()
    try:
        name_row = conn.execute("SELECT name FROM users WHERE loginname=?", (loginname,)).fetchone()
        if not name_row or not name_row['name'] or name_row['name'] == loginname:
            flash('该用户未完成姓名绑定，请先让其在成员页完善姓名后再授予。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))

        conn.execute('BEGIN IMMEDIATE')
        acc = get_account(conn, loginname)
        if acc and acc['status'] == 'graduated':
            conn.rollback()
            flash('该同学已毕业，不能再授予经费。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        new = (acc['balance'] if acc else 0) + amount
        if acc:
            conn.execute("UPDATE academic_grant_accounts SET balance=? WHERE loginname=?",
                         (new, loginname))
        else:
            conn.execute("INSERT INTO academic_grant_accounts (loginname, balance) VALUES (?, ?)",
                         (loginname, new))
        add_record(conn, loginname, 'grant', amount, note)
        conn.commit()
        flash(f'已为 {name_row["name"]} 授予 {amount} 元经费，当前余额 {new} 元。')
    except sqlite3.IntegrityError:
        conn.rollback()
        flash('操作失败：违反数据约束。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))


@academic_grant_bp.route('/adjust/<loginname>', methods=['POST'])
@login_required
@admin_required
def adjust(loginname):
    """管理员调整扣减（仅负向变动）：输入正数扣减额度，落账为负的 adjust 流水。
    追加统一走页头「授予经费」表单（grant）。"""
    scope = form_scope()
    amount = request.form.get('amount', type=int)
    note = (request.form.get('note') or '').strip()

    if amount is None or amount <= 0:
        flash('调整额度必须是正整数（填写扣减额度，系统记为负调整）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if amount > MAX_AMOUNT:
        flash(f'调整额度超出允许范围（单次最多 {MAX_AMOUNT:,} 元）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if not note:
        flash('请填写有效的调整说明。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if len(note) > MAX_NOTE_LEN:
        flash(f'说明不能超过 {MAX_NOTE_LEN} 字。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))

    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        acc = get_account(conn, loginname)
        if not acc:
            conn.rollback()
            flash('该同学还没有经费账户，无法调整。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if acc['status'] == 'graduated':
            conn.rollback()
            flash('该同学已毕业，不能调整经费。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if amount > acc['balance']:
            conn.rollback()
            flash(f'调整失败：调整额度不得超过当前余额（当前余额 {acc["balance"]} 元）。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        new = acc['balance'] - amount
        conn.execute("UPDATE academic_grant_accounts SET balance=? WHERE loginname=?",
                     (new, loginname))
        add_record(conn, loginname, 'adjust', -amount, note)
        conn.commit()
        flash(f'已调整 {display_name(conn, loginname)} 的经费 -{amount} 元，当前余额 {new} 元。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))


@academic_grant_bp.route('/usage', methods=['POST'])
@login_required
def usage():
    """本人申报经费使用（参加完活动、完成报销后），直接扣减余额，无需审批。"""
    scope = form_scope()
    user = session['user']
    amount = request.form.get('amount', type=int)
    note = (request.form.get('note') or '').strip()

    if amount is None or amount <= 0:
        flash('使用额度必须是正整数。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if amount > MAX_AMOUNT:
        flash(f'使用额度超出允许范围（单次最多 {MAX_AMOUNT:,} 元）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if not note:
        flash('请填写使用说明（如活动名称、报销内容）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if len(note) > MAX_NOTE_LEN:
        flash(f'使用说明不能超过 {MAX_NOTE_LEN} 字。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))

    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        acc = get_account(conn, user)
        if not acc:
            conn.rollback()
            flash('你还没有经费账户，请联系管理员授予。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if acc['status'] == 'graduated':
            conn.rollback()
            flash('你已毕业，不能申报经费使用。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if acc['balance'] - amount < -OVERDRAFT_LIMIT:
            conn.rollback()
            flash(f'余额不足：当前余额 {acc["balance"]} 元（可透支至 -{OVERDRAFT_LIMIT} 元），本次申报 {amount} 元。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        new = acc['balance'] - amount
        conn.execute("UPDATE academic_grant_accounts SET balance=? WHERE loginname=?", (new, user))
        add_record(conn, user, 'usage', -amount, note)
        conn.commit()
        flash(f'已申报使用 {amount} 元，剩余余额 {new} 元。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))


@academic_grant_bp.route('/transfer', methods=['POST'])
@login_required
def transfer():
    """转赠额度给同学，手续费 10%：received = (amount*9+5)//10（实收 90% 四舍五入到整数），
    转出额不得超过当前余额；单事务双扣双记防单边账。"""
    scope = form_scope()
    user = session['user']
    to = (request.form.get('to') or '').strip()
    amount = request.form.get('amount', type=int)
    note = (request.form.get('note') or '').strip()

    if amount is None or amount <= 0:
        flash('转赠额度必须是正整数。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if amount > MAX_AMOUNT:
        flash(f'转赠额度超出允许范围（单次最多 {MAX_AMOUNT:,} 元）。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    received = (amount * 9 + 5) // 10   # 实收 90%，四舍五入到整数（half-up）
    if len(note) > MAX_NOTE_LEN:
        flash(f'转赠说明不能超过 {MAX_NOTE_LEN} 字。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if to == user:
        flash('不能向自己转赠额度。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))
    if to not in VALID_USERS:
        flash('收款同学不存在。')
        return redirect(url_for('academic_grant.grant_page', scope=scope))

    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        from_acc = get_account(conn, user)
        if not from_acc:
            conn.rollback()
            flash('你还没有经费账户，无法转赠。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if from_acc['status'] == 'graduated':
            conn.rollback()
            flash('你已毕业，不能转赠额度。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        to_acc = get_account(conn, to)
        if not to_acc:
            conn.rollback()
            flash('对方还没有经费账户，无法转赠（需管理员先为其开户）。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if to_acc['status'] == 'graduated':
            conn.rollback()
            flash('对方已毕业，不能接收转赠。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        if amount > from_acc['balance']:
            conn.rollback()
            flash(f'余额不足：当前余额 {from_acc["balance"]} 元，转出额不得超过当前余额。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))

        fee = amount - received
        new_from = from_acc['balance'] - amount
        new_to = to_acc['balance'] + received
        conn.execute("UPDATE academic_grant_accounts SET balance=? WHERE loginname=?",
                     (new_from, user))
        conn.execute("UPDATE academic_grant_accounts SET balance=? WHERE loginname=?",
                     (new_to, to))
        add_record(conn, user, 'transfer_out', -amount, note, related_loginname=to)
        fee_note = f'（转赠 {amount} 元，手续费 {fee} 元）'
        add_record(conn, to, 'transfer_in', received, (note + fee_note) if note else fee_note,
                   related_loginname=user)
        conn.commit()
        to_display = display_name(conn, to)
        flash(f'已向 {to_display} 转赠 {amount} 元，对方实到 {received} 元（手续费 {fee} 元）；'
              f'你的余额剩余 {new_from} 元。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))


@academic_grant_bp.route('/graduate/<loginname>', methods=['POST'])
@login_required
@admin_required
def graduate(loginname):
    """管理员标记毕业：账户冻结（只读），记录保留。WHERE 守卫保证幂等。"""
    scope = form_scope()
    conn = get_db_connection()
    try:
        acc = get_account(conn, loginname)
        if not acc:
            flash('该同学还没有经费账户，无需毕业操作。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        cur = conn.execute(
            "UPDATE academic_grant_accounts SET status='graduated', "
            "graduated_at=datetime('now','localtime') WHERE loginname=? AND status='active'",
            (loginname,))
        if cur.rowcount == 0:
            conn.rollback()
            flash('该同学已是毕业状态。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        conn.commit()
        flash(f'已将 {display_name(conn, loginname)} 标记为毕业，其经费版块转为只读（记录仍保留）。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))


@academic_grant_bp.route('/revoke/<loginname>', methods=['POST'])
@login_required
@admin_required
def revoke(loginname):
    """管理员撤销毕业（复活）：status 转回 active 且 graduated_at 置空。"""
    scope = form_scope()
    conn = get_db_connection()
    try:
        acc = get_account(conn, loginname)
        if not acc:
            flash('该同学还没有经费账户。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        cur = conn.execute(
            "UPDATE academic_grant_accounts SET status='active', graduated_at=NULL "
            "WHERE loginname=? AND status='graduated'", (loginname,))
        if cur.rowcount == 0:
            conn.rollback()
            flash('该同学不是毕业状态，无需撤销。')
            return redirect(url_for('academic_grant.grant_page', scope=scope))
        conn.commit()
        flash(f'已撤销 {display_name(conn, loginname)} 的毕业标记，恢复经费操作。')
    finally:
        conn.close()
    return redirect(url_for('academic_grant.grant_page', scope=scope))
