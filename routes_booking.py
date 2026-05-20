from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta
from db import get_db_connection
from auth import login_required
from helpers import compute_upcoming_instances

booking_bp = Blueprint('booking', __name__, url_prefix='/booking')


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
                'bookers': [b['booker'] for b in bookers]
            })
        display_slots.append({
            'slot': dict(slot),
            'instances': instance_data
        })

    my_bookings = conn.execute(
        """SELECT b.id, b.instance_date, s.title, s.slot_type, s.start_hour, s.end_hour,
                  s.specific_date, s.day_of_week, s.publisher
           FROM bookings b JOIN booking_slots s ON b.slot_id=s.id
           WHERE b.booker=? AND b.status='active' ORDER BY b.instance_date""",
        (current_user,)
    ).fetchall()

    my_slots = conn.execute(
        "SELECT * FROM booking_slots WHERE publisher=? AND status='active' ORDER BY created_at DESC",
        (current_user,)
    ).fetchall()

    conn.close()

    one_time_slots = [s for s in display_slots if s['slot']['slot_type'] == 'one_time']
    recurring_slots = [s for s in display_slots if s['slot']['slot_type'] == 'recurring']
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
    if request.method == 'POST':
        current_user = session['user']
        slot_type = request.form.get('slot_type')
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        start_hour = int(request.form.get('start_hour'))
        end_hour = int(request.form.get('end_hour'))
        capacity = int(request.form.get('capacity', 1))

        if not title or len(title) > 50:
            flash('标题不能为空且不超过50字')
            return redirect(url_for('booking.booking_publish'))
        if end_hour <= start_hour:
            flash('结束时间必须大于开始时间')
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
            if specific_date < datetime.now().strftime('%Y-%m-%d'):
                flash('一次性预约日期必须是未来日期')
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

    return render_template('booking_publish.html', page='booking')


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