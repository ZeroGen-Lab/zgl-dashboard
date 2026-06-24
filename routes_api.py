from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from db import get_db_connection
from auth import token_required, checkin_ip_required
from helpers import compute_week_key, save_weekly_plan, save_daily_completion

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
    if not data or 'uid' not in data:
        return jsonify({'success': False, 'message': '缺少 uid 参数'}), 400

    uid = data['uid']
    # 新格式：items 列表；旧格式：单个 content -> 按'\n' split的列表（向后兼容）
    if 'items' in data:
        items = data['items']
        if not isinstance(items, list):
            return jsonify({'success': False, 'message': 'items 必须是列表'}), 400
    elif 'content' in data:
        items = data['content'].split('\n')
    else:
        return jsonify({'success': False, 'message': '缺少 items 或 content 参数'}), 400

    week_key = compute_week_key(datetime.now())
    if week_key is None:
        return jsonify({'success': False, 'message': '当前不在提交时间窗口内（周六至周一18点）'}), 400

    try:
        save_weekly_plan(uid, week_key, items)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    return jsonify({'success': True, 'message': '每周计划已提交', 'uid': uid, 'week_key': week_key})


@api_bp.route('/daily_completion', methods=['POST'])
@token_required
def daily_completion():
    data = request.get_json()
    if not data or 'uid' not in data:
        return jsonify({'success': False, 'message': '缺少 uid 参数'}), 400

    uid = data['uid']
    # review 必填；兼容老版本 content（当作 review）
    if 'review' in data:
        review = data['review'] or ''
    elif 'content' in data:
        review = data['content'] or ''
    else:
        return jsonify({'success': False, 'message': '缺少 review 参数'}), 400
    todo = data.get('todo') or ''

    try:
        date = save_daily_completion(uid, review, todo)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400

    return jsonify({'success': True, 'message': '每日完成情况已提交', 'uid': uid, 'date': date})