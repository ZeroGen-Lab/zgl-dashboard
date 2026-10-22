"""Coding Plan Key 领域逻辑：配置条目校验、脱敏展示与领取登记。

领取记录存于用量镜像库的 coding_plan_claims 表（经 db_usage 读写）；
完整 Key 值只在私有 .config.yml，本模块只处理登记与展示。
"""
import re
import sqlite3
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


class ClaimConflict(ValueError):
    pass


def change_claim(path, primary_path, key_id, username, action, exclusive_ids=()):
    initialize_usage_mirror(path, primary_path)
    with closing(connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if action == 'claim':
            if exclusive_ids:
                placeholders = ','.join('?' for _ in exclusive_ids)
                other = conn.execute(
                    f'SELECT 1 FROM coding_plan_claims WHERE username=? AND key_id<>? '
                    f'AND key_id IN ({placeholders}) LIMIT 1',
                    (username, key_id, *exclusive_ids)).fetchone()
                if other:
                    raise ClaimConflict('每人只能登记一个智谱 Key，请先停止使用已登记的 Key。')
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
