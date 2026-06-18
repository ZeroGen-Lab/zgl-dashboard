from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from auth import login_required, admin_required, is_admin
from db import get_db_connection

okr_bp = Blueprint('okr', __name__, url_prefix='/okr')


# --- 辅助函数 ---

def get_current_cycle_key():
    """根据当前月份判断学期。3-8月=Spring，9-2月=Autumn。"""
    today = datetime.now()
    year = str(today.year)
    season = 'Spring' if 3 <= today.month <= 8 else 'Autumn'
    return year, season, f"{year}-{season}"


def get_available_cycle_keys():
    """返回当前周期 + 前后各一个周期，供下拉选择。"""
    today = datetime.now()
    current_year = today.year
    current_season = 'Spring' if 3 <= today.month <= 8 else 'Autumn'
    current_key = f"{current_year}-{current_season}"

    if current_season == 'Spring':
        prev = f"{current_year - 1}-Autumn"
    else:
        prev = f"{current_year}-Spring"

    if current_season == 'Autumn':
        nxt = f"{current_year + 1}-Spring"
    else:
        nxt = f"{current_year}-Autumn"

    return [
        {'cycle_key': k, 'label': k, 'is_current': k == current_key}
        for k in [prev, current_key, nxt]
    ]


def calc_objective_progress(objective_id):
    """计算目标进度 = 所有 active KR 进度的平均值。"""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT progress FROM okr_key_results WHERE objective_id=? AND status='active'",
        (objective_id,)
    ).fetchall()
    conn.close()
    if not rows:
        return 0
    return sum(r['progress'] for r in rows) // len(rows)


# --- 主页面 ---

@okr_bp.route('/')
@login_required
def okr_page():
    selected_cycle = request.args.get('cycle') or get_current_cycle_key()[2]
    cycles = get_available_cycle_keys()

    conn = get_db_connection()
    # fetch cycle info: id, status
    cycle = conn.execute(
        "SELECT * FROM okr_cycles WHERE cycle_key=?", (selected_cycle,)
    ).fetchone()

    objectives = []
    if cycle:
        is_brainstorming = cycle['status'] == 'brainstorming'

        # Objective 列表（含 KR）：
        # - 脑暴阶段：展示所有 objective 和 KR（包含 rejected）
        # - planning/active/closed：只展示 approved objective，所有 KR 均展示（无 cancelled 状态）
        if is_brainstorming:
            obj_filter = ""
        else:
            obj_filter = "AND status='approved' "

        obj_rows = conn.execute(
            f"SELECT * FROM okr_objectives WHERE cycle_id=? {obj_filter}"
            "ORDER BY created_at",
            (cycle['id'],)
        ).fetchall()
        for o in obj_rows:
            krs = conn.execute(
                "SELECT * FROM okr_key_results WHERE objective_id=? "
                "ORDER BY created_at",
                (o['id'],)
            ).fetchall()
            kr_list = []
            for kr in krs:
                milestones = conn.execute(
                    "SELECT * FROM okr_kr_milestones WHERE kr_id=? ORDER BY created_at",
                    (kr['id'],)
                ).fetchall()
                kr_list.append({
                    **dict(kr),
                    'milestones': [dict(m) for m in milestones],
                })
            o_progress = calc_objective_progress(o['id'])
            objectives.append({
                **dict(o),
                'krs': kr_list,
                'progress': o_progress,
            })
    conn.close()

    return render_template('okr.html', page='okr',
                           cycle=dict(cycle) if cycle else None,
                           cycles=cycles, selected_cycle=selected_cycle,
                           objectives=objectives,
                           current_user=session['user'],
                           is_admin=is_admin())


# --- 周期管理（管理员）---

@okr_bp.route('/cycle/start', methods=['POST'])
@login_required
@admin_required
def cycle_start():
    """
    Create a new cycle for the user-selected cycle_key, if it doesn't already exist.
    The new cycle starts in 'brainstorming' status.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    if not cycle_key:
        flash('No cycle key provided.')
        return redirect(url_for('okr.okr_page'))
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id FROM okr_cycles WHERE cycle_key=?", (cycle_key,)
    ).fetchone()
    if existing:
        flash(f'OKR cycle {cycle_key} already exists.')
    else:
        conn.execute(
            "INSERT INTO okr_cycles (cycle_key, created_by) VALUES (?, ?)",
            (cycle_key, session['user'])
        )
        conn.commit()
        flash(f'OKR cycle {cycle_key} created (status: brainstorming).')
    conn.close()
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/cycle/change_status', methods=['POST'])
@login_required
@admin_required
def cycle_change_status():
    """ 
    Change the status of an OKR cycle.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    new_status = request.form.get('status', '').strip()
    if new_status not in ('brainstorming', 'planning', 'active', 'closed'):
        flash('Invalid status value: expected brainstorming, planning, active, or closed.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    conn.execute(
        "UPDATE okr_cycles SET status=? WHERE cycle_key=?",
        (new_status, cycle_key)
    )
    conn.commit()
    conn.close()
    flash(f'The status of OKR cycle {cycle_key} has been changed to {new_status}.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


# --- 目标管理 ---

@okr_bp.route('/objective/draft', methods=['POST'])
@login_required
def objective_draft():
    """
    Submit a new draft objective within an OKR cycle in brainstorming phase.
    The draft will have status 'draft'.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    if not title:
        flash('Objective title cannot be empty.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    cycle = conn.execute(
        "SELECT * FROM okr_cycles WHERE cycle_key=? AND status='brainstorming'",
        (cycle_key,)
    ).fetchone()
    if not cycle:
        flash('The current cycle is not in the brainstorming phase, and drafts cannot be submitted.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "INSERT INTO okr_objectives (cycle_id, title, description, status, proposed_by) "
        "VALUES (?, ?, ?, 'draft', ?)",
        (cycle['id'], title, description, session['user'])
    )
    conn.commit()
    conn.close()
    flash('Draft submitted successfully.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/objective/reject_draft/<int:obj_id>', methods=['POST'])
@login_required
@admin_required
def objective_reject_draft(obj_id):
    """
    Reject a draft objective by setting its status to 'rejected'.
    Only objectives in 'draft' status can be rejected.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    conn = get_db_connection()
    conn.execute(
        "UPDATE okr_objectives SET status='rejected' WHERE id=? AND status='draft'",
        (obj_id,)
    )
    conn.commit()
    conn.close()
    flash('Draft rejected successfully.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/objective/approve/<int:obj_id>', methods=['POST'])
@login_required
@admin_required
def objective_approve(obj_id):
    """
    Promote a draft objective to 'approved' status.
    Only objectives in 'draft' status can be approved.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    conn = get_db_connection()
    conn.execute(
        "UPDATE okr_objectives SET status='approved' WHERE id=? AND status='draft'",
        (obj_id,)
    )
    conn.commit()
    conn.close()
    flash('Draft approved as formal Objective.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


# --- 关键结果管理 ---

@okr_bp.route('/kr/add', methods=['POST'])
@login_required
def kr_add():
    """
    Add a new key result under an approved objective.
    The new KR starts with progress=0 and status='active'.
    """
    objective_id = request.form.get('objective_id', type=int)
    cycle_key = request.form.get('cycle_key', '').strip()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    uid = request.form.get('uid', session['user']).strip()
    if not title:
        flash('Key result title cannot be empty.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    obj = conn.execute(
        "SELECT o.* FROM okr_objectives o "
        "JOIN okr_cycles c ON o.cycle_id=c.id "
        "WHERE o.id=? AND o.status='approved' AND c.status='planning'",
        (objective_id,)
    ).fetchone()
    if not obj:
        flash('This objective cannot have key results added (must be in planning cycle and the objective must be approved).')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "INSERT INTO okr_key_results (objective_id, uid, title, description) VALUES (?, ?, ?, ?)",
        (objective_id, uid, title, description)
    )
    conn.commit()
    conn.close()
    flash('Key result added successfully.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/update_progress/<int:kr_id>', methods=['POST'])
@login_required
def kr_update_progress(kr_id):
    """
    Update the progress of a key result. Allowed in planning and active phases.
    Only the KR owner can update progress. Progress must be an integer 0-100.
    """
    progress = request.form.get('progress', type=int)
    cycle_key = request.form.get('cycle_key', '').strip()
    if progress is None or progress < 0 or progress > 100:
        flash('Progress value must be between 0 and 100.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    kr = conn.execute(
        "SELECT uid FROM okr_key_results WHERE id=?", (kr_id,)
    ).fetchone()
    if not kr:
        flash('Key result does not exist.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['uid'] != session['user']:
        flash('You can only update progress for key results you are responsible for.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "UPDATE okr_key_results SET progress=? WHERE id=? AND status IN ('active','pending_edit')",
        (progress, kr_id)
    )
    conn.commit()
    conn.close()
    flash('Progress updated successfully.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/edit/<int:kr_id>', methods=['POST'])
@login_required
def kr_edit(kr_id):
    """
    Free edit of KR title/description. Only allowed when cycle is in 'planning' phase.
    Only the KR owner can edit.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    if not title:
        flash('Key result title cannot be empty.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    kr = conn.execute(
        "SELECT k.*, c.status AS cycle_status FROM okr_key_results k "
        "JOIN okr_objectives o ON k.objective_id=o.id "
        "JOIN okr_cycles c ON o.cycle_id=c.id "
        "WHERE k.id=?", (kr_id,)
    ).fetchone()
    if not kr:
        flash('Key result does not exist.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['cycle_status'] != 'planning':
        flash('Free edit is only allowed in planning phase. Use Request Edit in active phase.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['uid'] != session['user']:
        flash('You can only edit key results you are responsible for.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "UPDATE okr_key_results SET title=?, description=? WHERE id=?",
        (title, description, kr_id)
    )
    conn.commit()
    conn.close()
    flash('Key result updated.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/add_milestone/<int:kr_id>', methods=['POST'])
@login_required
def kr_add_milestone(kr_id):
    """
    Add a milestone to a key result. Only the KR owner can add milestones.
    Milestone description cannot be empty.
    """
    description = request.form.get('description', '').strip()
    cycle_key = request.form.get('cycle_key', '').strip()
    if not description:
        flash('Milestone description cannot be empty.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    kr = conn.execute(
        "SELECT uid FROM okr_key_results WHERE id=?", (kr_id,)
    ).fetchone()
    if not kr:
        flash('Key result does not exist.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['uid'] != session['user']:
        flash('You can only add milestones to key results you are responsible for.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "INSERT INTO okr_kr_milestones (kr_id, description) VALUES (?, ?)",
        (kr_id, description)
    )
    conn.commit()
    conn.close()
    flash('Milestone added successfully.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/request_edit/<int:kr_id>', methods=['POST'])
@login_required
def kr_request_edit(kr_id):
    """
    Submit an edit request for a KR in active phase. Stores proposed title/description
    as pending_* and sets status to 'pending_edit' for admin review.
    Only the KR owner can submit an edit request.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    if not title:
        flash('Key result title cannot be empty.')
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn = get_db_connection()
    kr = conn.execute(
        "SELECT k.*, c.status AS cycle_status FROM okr_key_results k "
        "JOIN okr_objectives o ON k.objective_id=o.id "
        "JOIN okr_cycles c ON o.cycle_id=c.id "
        "WHERE k.id=?", (kr_id,)
    ).fetchone()
    if not kr:
        flash('Key result does not exist.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['cycle_status'] != 'active':
        flash('Edit requests are only available in active phase. Use free edit in planning phase.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['status'] != 'active':
        flash('This key result already has a pending edit request.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    if kr['uid'] != session['user']:
        flash('You can only request edits for key results you are responsible for.')
        conn.close()
        return redirect(url_for('okr.okr_page', cycle=cycle_key))
    conn.execute(
        "UPDATE okr_key_results SET status='pending_edit', pending_title=?, pending_description=? "
        "WHERE id=?",
        (title, description, kr_id)
    )
    conn.commit()
    conn.close()
    flash('Edit request submitted, awaiting administrator review.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/approve_edit/<int:kr_id>', methods=['POST'])
@login_required
@admin_required
def kr_approve_edit(kr_id):
    """
    Approve a pending edit request: apply pending_title/pending_description to the actual
    columns, clear pending fields, and revert status to 'active'.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    conn = get_db_connection()
    conn.execute(
        "UPDATE okr_key_results "
        "SET title=pending_title, description=pending_description, "
        "    pending_title='', pending_description='', status='active' "
        "WHERE id=? AND status='pending_edit'",
        (kr_id,)
    )
    conn.commit()
    conn.close()
    flash('Edit request approved and applied.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))


@okr_bp.route('/kr/reject_edit/<int:kr_id>', methods=['POST'])
@login_required
@admin_required
def kr_reject_edit(kr_id):
    """
    Reject a pending edit request: discard pending fields and revert status to 'active'.
    """
    cycle_key = request.form.get('cycle_key', '').strip()
    conn = get_db_connection()
    conn.execute(
        "UPDATE okr_key_results "
        "SET pending_title='', pending_description='', status='active' "
        "WHERE id=? AND status='pending_edit'",
        (kr_id,)
    )
    conn.commit()
    conn.close()
    flash('Edit request rejected.')
    return redirect(url_for('okr.okr_page', cycle=cycle_key))
