import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
from config import DINGTALK_WEBHOOK_URL, DINGTALK_SECRET


def _build_signed_url():
    """构建带加签参数的钉钉 webhook URL"""
    if not DINGTALK_SECRET:
        return DINGTALK_WEBHOOK_URL
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f'{timestamp}\n{DINGTALK_SECRET}'
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = '&' if '?' in DINGTALK_WEBHOOK_URL else '?'
    return f'{DINGTALK_WEBHOOK_URL}{sep}timestamp={timestamp}&sign={sign}'


def send_dingtalk_markdown(title, text):
    """发送钉钉 Markdown 消息。title 用于通知栏，text 为消息正文。"""
    if not DINGTALK_WEBHOOK_URL:
        return False
    url = _build_signed_url()
    payload = {
        'msgtype': 'markdown',
        'markdown': {'title': title, 'text': text}
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200 and resp.json().get('errcode') == 0
    except Exception:
        return False
