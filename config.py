import os
import re
import sqlite3
import sys
import yaml
from contextlib import closing
from pathlib import Path

from db_usage import connect, initialize as initialize_usage_mirror


KEY_ID_RE = re.compile(r'^[a-zA-Z0-9_.-]{1,80}$')
CODING_PLAN_PLATFORMS = {'deepseek', 'zhipu'}


def validate_keys(keys):
    if not isinstance(keys, list):
        raise ValueError('Coding Plan keys must be a list')
    seen = set()
    for key in keys:
        if not isinstance(key, dict) or not isinstance(key.get('id'), str) or not KEY_ID_RE.fullmatch(key['id']):
            raise ValueError('Coding Plan key id is invalid')
        if key['id'] in seen:
            raise ValueError('Coding Plan key ids must be unique')
        seen.add(key['id'])
        if key.get('platform') not in CODING_PLAN_PLATFORMS:
            raise ValueError('Coding Plan platform must be deepseek or zhipu')
        if not isinstance(key.get('label'), str) or not key['label'].strip():
            raise ValueError('Coding Plan key label is missing')
        if (not isinstance(key.get('secret'), str)
                or (key['secret'] and not key['secret'].strip())
                or key['secret'].startswith('REPLACE_WITH_')):
            raise ValueError('Coding Plan key secret is invalid')


def masked(secret):
    if len(secret) < 12:
        return '••••••••'
    return secret[:4] + '••••••••' + secret[-4:]


def claim_status(path, username):
    if not path or not Path(path).is_file():
        return {}, set()
    try:
        with closing(connect(path, readonly=True)) as conn:
            counts = {row['key_id']: row['user_count'] for row in conn.execute(
                'SELECT key_id, COUNT(*) user_count FROM coding_plan_claims GROUP BY key_id')}
            claimed = {row['key_id'] for row in conn.execute(
                'SELECT key_id FROM coding_plan_claims WHERE username=?', (username,))}
            return counts, claimed
    except sqlite3.Error:
        return {}, set()


def change_claim(path, primary_path, key_id, username, action):
    initialize_usage_mirror(path, primary_path)
    with closing(connect(path)) as conn, conn:
        if action == 'claim':
            conn.execute('INSERT OR IGNORE INTO coding_plan_claims(key_id,username) VALUES (?,?)',
                         (key_id, username))
        elif action == 'release':
            conn.execute('DELETE FROM coding_plan_claims WHERE key_id=? AND username=?',
                         (key_id, username))
        claimed = conn.execute(
            'SELECT 1 FROM coding_plan_claims WHERE key_id=? AND username=?',
            (key_id, username)).fetchone() is not None
        count = conn.execute('SELECT COUNT(*) FROM coding_plan_claims WHERE key_id=?',
                             (key_id,)).fetchone()[0]
        return count, claimed

# --- 环境检测：测试环境必须显式指定独立数据库，不读取真实配置 ---
def get_env():
    requested = os.environ.get('ZGL_ENV')
    if requested in ('pre', 'test'):
        return requested
    if len(sys.argv) > 1 and sys.argv[1] in ('pre', 'test'):
        return sys.argv[1]
    return 'prod'


ENV = get_env()
_project_dir = os.path.dirname(os.path.abspath(__file__))


def cli_option(name):
    """Read options only when app.py is started directly, not by a WSGI server."""
    if Path(sys.argv[0]).name != 'app.py' or name not in sys.argv:
        return None
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv) or sys.argv[index + 1].startswith('--'):
        raise RuntimeError(f'{name} 缺少参数值')
    return sys.argv[index + 1]


if ENV == 'test':
    import secrets
    _test_db = os.environ.get('ZGL_TEST_DB_PATH')
    if not _test_db or not os.path.isabs(_test_db):
        raise RuntimeError('测试环境需要通过 ZGL_TEST_DB_PATH 指定独立数据库的绝对路径。')
    _effective = {
        'db_path': _test_db,
        'db_bak_dir': os.path.dirname(_test_db),
        'port': int(os.environ.get('ZGL_TEST_PORT', '5051')),
        'api_secret': 'zgl-local-test-only',
        'secret_key': secrets.token_hex(32),
        'allowed_checkin_ips': ['127.0.0.1', '::1'],
    }
    USERS_PATH = os.environ.get('ZGL_TEST_USERS_PATH', _test_db + '.users.txt')
else:
    _config_path = cli_option('--config') or os.path.join(_project_dir, '.config.yml')
    with open(_config_path, encoding='utf-8') as f:
        _config = yaml.safe_load(f)
    _server = _config['server']
    _effective = {**_server, **_config.get('pre', {})} if ENV == 'pre' else _server
    USERS_PATH = os.path.join(_project_dir, '.users.txt')

DB_PATH = _effective['db_path']
DB_BAK_DIR = _effective['db_bak_dir']
PORT = _effective['port']
API_SECRET = _effective['api_secret']
ALLOWED_CHECKIN_IPS = _effective['allowed_checkin_ips']
SECRET_KEY = _effective['secret_key']
DINGTALK_WEBHOOK_URL = _effective.get('dingtalk_webhook_url', '')
DINGTALK_SECRET = _effective.get('dingtalk_secret', '')
DEEPSEEK_API_KEY = _effective.get('deepseek_api_key', '')
DEEPSEEK_BASE_URL = _effective.get('deepseek_base_url', 'https://api.deepseek.com')
CODING_PLAN_KEYS = _effective.get('coding_plan_keys', [])
validate_keys(CODING_PLAN_KEYS)
LLM_USAGE_SYNC = _effective.get('llm_usage_sync', {})
if ENV == 'pre':
    _pre_usage = _config.get('pre', {}).get('llm_usage_sync', {})
    LLM_USAGE_SYNC = {**_server.get('llm_usage_sync', {}), **_pre_usage}
    if not _pre_usage.get('mirror_db_path'):
        LLM_USAGE_SYNC = {'enabled': False}
if ENV == 'test':
    LLM_USAGE_SYNC = {
        'enabled': False,
        'mirror_db_path': os.environ.get('ZGL_TEST_USAGE_DB_PATH', _test_db + '.usage.db'),
    }

flask_config = {
    'ENV_NAME': ENV,
    'DB_PATH': DB_PATH,
    'API_SECRET': API_SECRET,
    'DB_BAK_DIR': DB_BAK_DIR,
    'ALLOWED_CHECKIN_IPS': ALLOWED_CHECKIN_IPS,
    'SECRET_KEY': SECRET_KEY,
    'DINGTALK_WEBHOOK_URL': DINGTALK_WEBHOOK_URL,
    'DINGTALK_SECRET': DINGTALK_SECRET,
    'DEEPSEEK_API_KEY': DEEPSEEK_API_KEY,
    'DEEPSEEK_BASE_URL': DEEPSEEK_BASE_URL,
    'CODING_PLAN_KEYS': CODING_PLAN_KEYS,
    'LLM_USAGE_SYNC': LLM_USAGE_SYNC,
}
