from contextlib import closing

from flask import Blueprint, render_template, request, redirect, url_for, session
from auth import VALID_USERS
from db import get_db_connection

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username in VALID_USERS and password == f'{username}@ZGL':
            session['user'] = username
            # 登录即建档：users 表缺该账号时创建，name 以登录名占位，已有姓名不覆盖。
            with closing(get_db_connection()) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO users(loginname, name) VALUES (?, ?) "
                        "ON CONFLICT(loginname) DO NOTHING", (username, username))
            return redirect(url_for('dashboard.index'))
        return render_template('login.html', error='用户名或密码错误')
    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('auth.login'))
