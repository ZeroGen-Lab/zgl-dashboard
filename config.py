import os
import sys
import yaml

# --- 从 .config.yml 加载配置 ---
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.config.yml')
with open(_config_path) as f:
    _config = yaml.safe_load(f)

# --- 环境检测：sys.argv 或环境变量 ZGL_ENV ---
def get_env():
    if os.environ.get('ZGL_ENV') == 'pre':
        return 'pre'
    if len(sys.argv) > 1 and sys.argv[1] == 'pre':
        return 'pre'
    return 'prod'

ENV = get_env()

# --- 根据环境合并配置：pre 节覆盖 server 节，未指定字段回退到 server ---
_server = _config['server']
if ENV == 'pre':
    _pre = _config.get('pre', {})
    _effective = {**_server, **_pre}
else:
    _effective = _server

DB_PATH = _effective['db_path']
PORT = _effective['port']
API_SECRET = _effective['api_secret']
ALLOWED_CHECKIN_IPS = _effective['allowed_checkin_ips']
SECRET_KEY = _effective['secret_key']
DINGTALK_WEBHOOK_URL = _effective.get('dingtalk_webhook_url', '')
DINGTALK_SECRET = _effective.get('dingtalk_secret', '')
DEEPSEEK_API_KEY = _effective.get('deepseek_api_key', '')
DEEPSEEK_BASE_URL = _effective.get('deepseek_base_url', 'https://api.deepseek.com/v1')

flask_config = {
    'DB_PATH': DB_PATH,
    'API_SECRET': API_SECRET,
    'ALLOWED_CHECKIN_IPS': ALLOWED_CHECKIN_IPS,
    'SECRET_KEY': SECRET_KEY,
    'DINGTALK_WEBHOOK_URL': DINGTALK_WEBHOOK_URL,
    'DINGTALK_SECRET': DINGTALK_SECRET,
    'DEEPSEEK_API_KEY': DEEPSEEK_API_KEY,
    'DEEPSEEK_BASE_URL': DEEPSEEK_BASE_URL,
}