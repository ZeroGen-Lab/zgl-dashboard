import os
import sys
import yaml

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
    _config_path = os.path.join(_project_dir, '.config.yml')
    with open(_config_path) as f:
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
}