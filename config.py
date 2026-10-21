import os
import sys
import yaml
from pathlib import Path

from coding_plan import validate_keys

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


def _anchor(path):
    """相对路径锚定到项目目录"""
    if not path:
        return path
    p = Path(path)
    return str(p) if p.is_absolute() else str(Path(_project_dir) / p)


def cli_option(name):
    """Read options from direct app or manual sync commands, not a WSGI server."""
    if Path(sys.argv[0]).name not in ('app.py', 'usage_sync.py') or name not in sys.argv:
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
    _config_path = cli_option('--config') or  _anchor('.config.yml')
    with open(_config_path, encoding='utf-8') as f:
        _config = yaml.safe_load(f)
    _server = _config['server']
    _effective = {**_server, **_config.get('pre', {})} if ENV == 'pre' else _server
    USERS_PATH =  _anchor('.users.txt')

DB_PATH = _anchor(_effective['db_path'])
DB_BAK_DIR = _anchor(_effective['db_bak_dir'])
PORT = _effective['port']
API_SECRET = _effective['api_secret']
ALLOWED_CHECKIN_IPS = _effective['allowed_checkin_ips']
SECRET_KEY = _effective['secret_key']
MEMBER_TOKEN_SECRET = _effective.get('member_token_secret', '')
if not isinstance(MEMBER_TOKEN_SECRET, str):
    raise ValueError('member_token_secret must be a string')
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
if LLM_USAGE_SYNC.get('mirror_db_path'):
    LLM_USAGE_SYNC['mirror_db_path'] = _anchor(LLM_USAGE_SYNC['mirror_db_path'])

flask_config = {
    'ENV_NAME': ENV,
    'DB_PATH': DB_PATH,
    'API_SECRET': API_SECRET,
    'DB_BAK_DIR': DB_BAK_DIR,
    'ALLOWED_CHECKIN_IPS': ALLOWED_CHECKIN_IPS,
    'SECRET_KEY': SECRET_KEY,
    'MEMBER_TOKEN_SECRET': MEMBER_TOKEN_SECRET,
    'DINGTALK_WEBHOOK_URL': DINGTALK_WEBHOOK_URL,
    'DINGTALK_SECRET': DINGTALK_SECRET,
    'DEEPSEEK_API_KEY': DEEPSEEK_API_KEY,
    'DEEPSEEK_BASE_URL': DEEPSEEK_BASE_URL,
    'CODING_PLAN_KEYS': CODING_PLAN_KEYS,
    'LLM_USAGE_SYNC': LLM_USAGE_SYNC,
}
