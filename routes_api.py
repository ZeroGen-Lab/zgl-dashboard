from contextlib import closing
from flask import Blueprint, request, jsonify
from datetime import datetime
from db import get_db_connection
from auth import token_required, checkin_ip_required
from helpers import compute_week_key, save_weekly_plan, save_daily_completion

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _request_data():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象')
    return data


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{key} 必须是非空字符串')
    return value.strip()


def _resolve_account(data):
    """可信 Token 客户端可传账号；兼容旧客户端传卡号，不将卡号当作账号。"""
    if 'loginname' not in data and 'uid' not in data:
        raise ValueError('缺少 loginname 或 uid 参数')
    loginname = _text(data, 'loginname') if 'loginname' in data else None
    uid = _text(data, 'uid') if 'uid' in data else None
    with closing(get_db_connection()) as conn:
        if uid is not None:
            card = conn.execute(
                'SELECT loginname FROM user_cards WHERE uid=?', (uid,)).fetchone()
            if not card:
                raise ValueError('卡片未绑定账号，请先绑定')
            if loginname is not None and loginname != card['loginname']:
                raise ValueError('loginname 与卡片所属账号不一致')
            loginname = card['loginname']
        if not conn.execute('SELECT 1 FROM users WHERE loginname=?', (loginname,)).fetchone():
            raise ValueError('用户账号不存在。')
    return loginname, uid


def _error(exc):
    return jsonify({'success': False, 'message': str(exc)}), 400


@api_bp.route('/checkin', methods=['POST'])
@token_required
@checkin_ip_required
def checkin():
    try:
        data = _request_data()
        uid = _text(data, 'uid')
        timestamp = data.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        if not isinstance(timestamp, str):
            raise ValueError('timestamp 必须是 YYYY-MM-DD HH:MM:SS 格式')
        try:
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            raise ValueError('timestamp 必须是 YYYY-MM-DD HH:MM:SS 格式') from None
    except ValueError as exc:
        return _error(exc)

    with closing(get_db_connection()) as conn:
        with conn:
            # 归属查询与写入在同一写事务，避免期间被解绑/改绑。
            conn.execute('BEGIN IMMEDIATE')
            card = conn.execute(
                'SELECT loginname FROM user_cards WHERE uid=?', (uid,)).fetchone()
            loginname = card['loginname'] if card else None
            conn.execute(
                'INSERT INTO sign_ins (loginname, card_uid, timestamp) VALUES (?, ?, ?)',
                (loginname, uid, timestamp))
    return jsonify({'success': True, 'message': '签到成功', 'uid': uid,
                    'loginname': loginname, 'timestamp': timestamp})


@api_bp.route('/weekly_plan', methods=['POST'])
@token_required
def weekly_plan():
    try:
        data = _request_data()
        loginname, uid = _resolve_account(data)
        if 'items' in data:
            items = data['items']
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError('items 必须是字符串列表')
        elif 'content' in data:
            if not isinstance(data['content'], str):
                raise ValueError('content 必须是字符串')
            items = data['content'].split('\n')
        else:
            raise ValueError('缺少 items 或 content 参数')
        week_key = compute_week_key(datetime.now())
        if week_key is None:
            raise ValueError('当前不在提交时间窗口内（周六至周一18点）')
        save_weekly_plan(loginname, week_key, items)
    except ValueError as exc:
        return _error(exc)
    result = {'success': True, 'message': '每周计划已提交',
              'loginname': loginname, 'week_key': week_key}
    if uid is not None:
        result['uid'] = uid
    return jsonify(result)


@api_bp.route('/daily_completion', methods=['POST'])
@token_required
def daily_completion():
    try:
        data = _request_data()
        loginname, uid = _resolve_account(data)
        if 'review' in data:
            review = data['review']
        elif 'content' in data:
            review = data['content']
        else:
            raise ValueError('缺少 review 参数')
        todo = data.get('todo')
        if not isinstance(review, str) or (todo is not None and not isinstance(todo, str)):
            raise ValueError('review/content 和 todo 必须是字符串')
        date = save_daily_completion(loginname, review, todo)
    except ValueError as exc:
        return _error(exc)
    result = {'success': True, 'message': '每日完成情况已提交',
              'loginname': loginname, 'date': date}
    if uid is not None:
        result['uid'] = uid
    return jsonify(result)
