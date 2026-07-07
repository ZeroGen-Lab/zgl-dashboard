from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta
from db import get_db_connection
from auth import login_required
from helpers import compute_upcoming_instances, is_instance_expired

booking_bp = Blueprint('booking', __name__, url_prefix='/booking')

# 可选时段：6:00–22:00，整点与半点（步长 0.5）
TIME_OPTIONS = [6.0 + 0.5 * i for i in range(33)]  # 6.0 .. 22.0


def _default_form():
    today = datetime.now().strftime('%Y-%m-%d')
    return {
        'slot_type': 'recurring',
        'day_of_week': 0,
        'specific_date': today,
        'title': '',
        'description': '',
        'start_hour': 20.0,
        'end_hour': 21.0,
        'capacity': 1,
    }


def _validate_time(start_hour, end_hour):
    """校验起止时间在 6:00–22:00 且 end > start，返回错误信息或 None"""
    if not (6.0 <= start_hour <= 22.0 and 6.0 <= end_hour <= 22.0):
        return '时间范围须在 6:00–22:00 之间'
    if end_hour <= start_hour:
        return '结束时间必须大于开始时间'
    return None


def _slot_next_date(slot_dict):
    """排序用：返回 slot 下一次发生日期 (YYYY-MM-DD)。
    已过期的一次性活动用哨兵日期排到最后。"""
    if slot_dict.get('expired'):
        return '9999-12-31'
    insts = compute_upcoming_instances(slot_dict, n=1)
    return insts[0] if insts else '9999-12-31'


@booking_bp.route('/')
@login_required
def booking():
    current_user = session['user']
    conn = get_db_connection()
    slots = conn.execute(
        "SELECT * FROM booking_slots WHERE status='active' ORDER BY created_at DESC"
    ).fetchall()

    display_slots = []
    for slot in slots:
        instances = compute_upcoming_instances(slot)
        instance_data = []
        for inst_date in instances:
            booked_count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND instance_date=? AND status='active'",
                (slot['id'], inst_date)
            ).fetchone()[0]
            user_booking = conn.execute(
                "SELECT id FROM bookings WHERE slot_id=? AND instance_date=? AND booker=? AND status='active'",
                (slot['id'], inst_date, current_user)
            ).fetchone()
            bookers = conn.execute(
                "SELECT booker FROM bookings WHERE slot_id=? AND instance_date=? AND status='active'",
                (slot['id'], inst_date)
            ).fetchall()
            instance_data.append({
                'date': inst_date,
                'booked_count': booked_count,
                'remaining': slot['capacity'] - booked_count,
                'user_booked': user_booking is not None,
                'user_booking_id': user_booking['id'] if user_booking else None,
                'bookers': [b['booker'] for b in bookers],
                'expired': is_instance_expired(slot, inst_date)
            })
        # one_time 已过期（specific_date < 今天）不产生实例，从广场隐藏
        if not instance_data:
            continue
        display_slots.append({
            'slot': dict(slot),
            'instances': instance_data
        })

    my_bookings = conn.execute(
        """SELECT b.id, b.instance_date, s.title, s.slot_type, s.start_hour, s.end_hour,
                  s.specific_date, s.day_of_week, s.publisher
           FROM bookings b JOIN booking_slots s ON b.slot_id=s.id
           WHERE b.booker=? AND b.status='active' ORDER BY b.instance_date, s.start_hour""",
        (current_user,)
    ).fetchall()

    my_slot_rows = conn.execute(
        "SELECT * FROM booking_slots WHERE publisher=? AND status='active' ORDER BY created_at DESC",
        (current_user,)
    ).fetchall()
    my_slots = []
    for s in my_slot_rows:
        d = dict(s)
        d['expired'] = (d['slot_type'] == 'one_time' and d['specific_date']
                        and is_instance_expired(d, d['specific_date']))
        my_slots.append(d)
    my_slots.sort(key=lambda d: (_slot_next_date(d), d['start_hour']))

    conn.close()

    one_time_slots = [s for s in display_slots if s['slot']['slot_type'] == 'one_time']
    recurring_slots = [s for s in display_slots if s['slot']['slot_type'] == 'recurring']
    # 一次性活动按 (活动日期, 开始时间) 升序；越近越靠前
    one_time_slots.sort(key=lambda item: (item['slot']['specific_date'], item['slot']['start_hour']))
    # 长期预约按 (下一次实例日期, 开始时间) 升序；instances[0] 已是最近一次实例
    recurring_slots.sort(key=lambda item: (item['instances'][0]['date'], item['slot']['start_hour']))
    day_names = {0: '周一', 1: '周二', 2: '周三', 3: '周四', 4: '周五', 5: '周六', 6: '周日'}

    return render_template('booking.html', page='booking',
                           one_time_slots=one_time_slots,
                           recurring_slots=recurring_slots,
                           my_bookings=my_bookings,
                           my_slots=my_slots,
                           day_names=day_names,
                           current_user=current_user)


@booking_bp.route('/publish', methods=['GET', 'POST'])
@login_required
def booking_publish():
    today = datetime.now().strftime('%Y-%m-%d')
    if request.method == 'POST':
        current_user = session['user']
        slot_type = request.form.get('slot_type')
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        start_hour = float(request.form.get('start_hour'))
        end_hour = float(request.form.get('end_hour'))
        capacity = int(request.form.get('capacity', 1))

        if not title or len(title) > 50:
            flash('标题不能为空且不超过50字')
            return redirect(url_for('booking.booking_publish'))
        err = _validate_time(start_hour, end_hour)
        if err:
            flash(err)
            return redirect(url_for('booking.booking_publish'))

        conn = get_db_connection()

        if slot_type == 'recurring':
            day_of_week = int(request.form.get('day_of_week'))
            existing = conn.execute(
                "SELECT id FROM booking_slots WHERE publisher=? AND slot_type='recurring' AND day_of_week=? AND start_hour=? AND status='active'",
                (current_user, day_of_week, start_hour)
            ).fetchone()
            if existing:
                flash('你已经发布了一个相同时间的长期预约时段')
                conn.close()
                return redirect(url_for('booking.booking_publish'))
            conn.execute(
                "INSERT INTO booking_slots (publisher, slot_type, title, description, day_of_week, start_hour, end_hour, capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (current_user, 'recurring', title, description, day_of_week, start_hour, end_hour, capacity)
            )
        elif slot_type == 'one_time':
            specific_date = request.form.get('specific_date')
            if not specific_date:
                flash('请选择具体日期')
                conn.close()
                return redirect(url_for('booking.booking_publish'))
            if specific_date < today:
                flash('一次性预约日期必须是今天或未来日期')
                conn.close()
                return redirect(url_for('booking.booking_publish'))
            conn.execute(
                "INSERT INTO booking_slots (publisher, slot_type, title, description, start_hour, end_hour, specific_date, capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (current_user, 'one_time', title, description, start_hour, end_hour, specific_date, capacity)
            )

        conn.commit()
        conn.close()
        flash('时段发布成功!')
        return redirect(url_for('booking.booking'))

    return render_template('booking_publish.html', page='booking', editing=False,
                           form=_default_form(), time_options=TIME_OPTIONS, today=today)


@booking_bp.route('/edit/<int:slot_id>', methods=['GET', 'POST'])
@login_required
def booking_edit(slot_id):
    current_user = session['user']
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    slot = conn.execute(
        "SELECT * FROM booking_slots WHERE id=? AND publisher=? AND status='active'",
        (slot_id, current_user)
    ).fetchone()
    if not slot:
        flash('无法编辑该时段')
        conn.close()
        return redirect(url_for('booking.booking'))
    # 一次性活动已过结束时间则不可编辑
    if slot['slot_type'] == 'one_time' and slot['specific_date'] and is_instance_expired(slot, slot['specific_date']):
        flash('该活动已结束，无法编辑')
        conn.close()
        return redirect(url_for('booking.booking'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        start_hour = float(request.form.get('start_hour'))
        end_hour = float(request.form.get('end_hour'))
        capacity = int(request.form.get('capacity', 1))

        if not title or len(title) > 50:
            flash('标题不能为空且不超过50字')
            conn.close()
            return redirect(url_for('booking.booking_edit', slot_id=slot_id))
        err = _validate_time(start_hour, end_hour)
        if err:
            flash(err)
            conn.close()
            return redirect(url_for('booking.booking_edit', slot_id=slot_id))

        if slot['slot_type'] == 'recurring':
            day_of_week = int(request.form.get('day_of_week'))
            existing = conn.execute(
                "SELECT id FROM booking_slots WHERE publisher=? AND slot_type='recurring' "
                "AND day_of_week=? AND start_hour=? AND status='active' AND id!=?",
                (current_user, day_of_week, start_hour, slot_id)
            ).fetchone()
            if existing:
                flash('你已经发布了一个相同时间的长期预约时段')
                conn.close()
                return redirect(url_for('booking.booking_edit', slot_id=slot_id))
            conn.execute(
                "UPDATE booking_slots SET title=?, description=?, day_of_week=?, start_hour=?, end_hour=?, capacity=? WHERE id=?",
                (title, description, day_of_week, start_hour, end_hour, capacity, slot_id)
            )
        else:  # one_time
            specific_date = request.form.get('specific_date')
            if not specific_date:
                flash('请选择具体日期')
                conn.close()
                return redirect(url_for('booking.booking_edit', slot_id=slot_id))
            if specific_date < today:
                flash('一次性预约日期必须是今天或未来日期')
                conn.close()
                return redirect(url_for('booking.booking_edit', slot_id=slot_id))
            conn.execute(
                "UPDATE booking_slots SET title=?, description=?, specific_date=?, start_hour=?, end_hour=?, capacity=? WHERE id=?",
                (title, description, specific_date, start_hour, end_hour, capacity, slot_id)
            )

        conn.commit()
        conn.close()
        flash('时段已更新!')
        return redirect(url_for('booking.booking'))

    form = {
        'slot_type': slot['slot_type'],
        'day_of_week': slot['day_of_week'] or 0,
        'specific_date': slot['specific_date'] or today,
        'title': slot['title'],
        'description': slot['description'] or '',
        'start_hour': slot['start_hour'],
        'end_hour': slot['end_hour'],
        'capacity': slot['capacity'],
    }
    conn.close()
    return render_template('booking_publish.html', page='booking', editing=True, slot=slot,
                           form=form, time_options=TIME_OPTIONS, today=today)


@booking_bp.route('/book/<int:slot_id>/<instance_date>', methods=['POST'])
@login_required
def booking_book(slot_id, instance_date):
    current_user = session['user']
    conn = get_db_connection()

    slot = conn.execute(
        "SELECT * FROM booking_slots WHERE id=? AND status='active'", (slot_id,)
    ).fetchone()
    if not slot:
        flash('该预约时段不存在或已取消')
        conn.close()
        return redirect(url_for('booking.booking'))

    if slot['publisher'] == current_user:
        flash('不能预约自己发布的时段')
        conn.close()
        return redirect(url_for('booking.booking'))

    instances = compute_upcoming_instances(slot)
    if instance_date not in instances:
        flash('该日期不在可预约范围内')
        conn.close()
        return redirect(url_for('booking.booking'))

    if is_instance_expired(slot, instance_date):
        flash('该时段已结束')
        conn.close()
        return redirect(url_for('booking.booking'))

    booked_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND instance_date=? AND status='active'",
        (slot_id, instance_date)
    ).fetchone()[0]
    if booked_count >= slot['capacity']:
        flash('该时段已被预约满')
        conn.close()
        return redirect(url_for('booking.booking'))

    existing = conn.execute(
        "SELECT id FROM bookings WHERE slot_id=? AND instance_date=? AND booker=? AND status='active'",
        (slot_id, instance_date, current_user)
    ).fetchone()
    if existing:
        flash('你已经预约了该时段')
        conn.close()
        return redirect(url_for('booking.booking'))

    conn.execute(
        "INSERT INTO bookings (slot_id, booker, instance_date) VALUES (?, ?, ?)",
        (slot_id, current_user, instance_date)
    )
    conn.commit()
    conn.close()
    flash('预约成功!')
    return redirect(url_for('booking.booking'))


@booking_bp.route('/cancel/<int:booking_id>', methods=['POST'])
@login_required
def booking_cancel(booking_id):
    current_user = session['user']
    conn = get_db_connection()
    booking = conn.execute(
        "SELECT * FROM bookings WHERE id=? AND booker=? AND status='active'",
        (booking_id, current_user)
    ).fetchone()
    if not booking:
        flash('无法取消该预约')
        conn.close()
        return redirect(url_for('booking.booking'))
    conn.execute(
        "UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,)
    )
    conn.commit()
    conn.close()
    flash('已取消预约')
    return redirect(url_for('booking.booking'))


@booking_bp.route('/cancel_slot/<int:slot_id>', methods=['POST'])
@login_required
def booking_cancel_slot(slot_id):
    current_user = session['user']
    conn = get_db_connection()
    slot = conn.execute(
        "SELECT * FROM booking_slots WHERE id=? AND publisher=? AND status='active'",
        (slot_id, current_user)
    ).fetchone()
    if not slot:
        flash('无法取消该时段')
        conn.close()
        return redirect(url_for('booking.booking'))
    conn.execute(
        "UPDATE booking_slots SET status='cancelled' WHERE id=?", (slot_id,)
    )
    conn.execute(
        "UPDATE bookings SET status='cancelled' WHERE slot_id=? AND status='active'",
        (slot_id,)
    )
    conn.commit()
    conn.close()
    flash('已取消发布的时段及相关预约')
    return redirect(url_for('booking.booking'))