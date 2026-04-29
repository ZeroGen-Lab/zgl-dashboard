from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from db import get_db_connection
from auth import token_required, checkin_ip_required
from helpers import compute_week_key

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/checkin', methods=['POST'])
@token_required
@checkin_ip_required
def checkin():
    data = request.get_json()
    if not data or 'uid' not in data:
        return jsonify({'success': False, 'message': '缺少 uid 参数'}), 400

    uid = data['uid']
    timestamp = data.get('timestamp', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    conn = get_db_connection()
    conn.execute("INSERT INTO sign_ins (uid, timestamp) VALUES (?, ?)", (uid, timestamp))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '签到成功', 'uid': uid, 'timestamp': timestamp})


@api_bp.route('/weekly_plan', methods=['POST'])
@token_required
def weekly_plan():
    data = request.get_json()
    if not data or 'uid' not in data or 'content' not in data:
        return jsonify({'success': False, 'message': '缺少 uid 或 content 参数'}), 400

    uid = data['uid']
    content = data['content']
    if len(content) > 200:
        return jsonify({'success': False, 'message': '每周计划内容不能超过200字'}), 400
    now = datetime.now()

    week_key = compute_week_key(now)
    if week_key is None:
        return jsonify({'success': False, 'message': '当前不在提交时间窗口内（周六至周一中午12点）'}), 400

    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO weekly_plans (uid, week_key, content) VALUES (?, ?, ?)",
                 (uid, week_key, content))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '每周计划已提交', 'uid': uid, 'week_key': week_key})


@api_bp.route('/daily_completion', methods=['POST'])
@token_required
def daily_completion():
    data = request.get_json()
    if not data or 'uid' not in data or 'content' not in data:
        return jsonify({'success': False, 'message': '缺少 uid 或 content 参数'}), 400

    uid = data['uid']
    content = data['content']
    if len(content) > 100:
        return jsonify({'success': False, 'message': '每日完成情况内容不能超过100字'}), 400
    date = data.get('date', datetime.now().strftime('%Y-%m-%d'))

    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO daily_completions (uid, date, content) VALUES (?, ?, ?)",
                 (uid, date, content))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '每日完成情况已提交', 'uid': uid, 'date': date})