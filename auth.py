import os
import hmac
import hashlib
from functools import wraps
from datetime import datetime, timedelta
from flask import request, jsonify, session, redirect, url_for
from config import API_SECRET, ALLOWED_CHECKIN_IPS

# --- 加载合法用户集 ---
VALID_USERS = set()
_users_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.users.txt')
if os.path.exists(_users_path):
    with open(_users_path) as f:
        for line in f:
            line = line.strip()
            if line:
                VALID_USERS.add(line)

# --- API Token 认证：HMAC-SHA256 按日轮换 ---
def generate_token(date_str):
    """根据日期字符串生成 HMAC-SHA256 token"""
    return hmac.new(API_SECRET.encode(), date_str.encode(), hashlib.sha256).hexdigest()

def verify_token(token):
    """验证 token，接受今天和昨天的 token（容时钟偏移）"""
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    return token in (generate_token(today), generate_token(yesterday))

def token_required(f):
    """API token 认证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
            if verify_token(token):
                return f(*args, **kwargs)
        return jsonify({'success': False, 'message': '认证失败，缺少或无效的 token'}), 401
    return decorated

def checkin_ip_required(f):
    """签到接口 IP 白名单装饰器，只允许端侧设备"""
    @wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.remote_addr
        if client_ip not in ALLOWED_CHECKIN_IPS:
            return jsonify({'success': False, 'message': f'IP {client_ip} 不在签到允许列表中'}), 403
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    """Session 登录认证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated