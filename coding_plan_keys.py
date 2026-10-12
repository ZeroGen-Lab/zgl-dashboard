"""Private Coding Plan keys and per-user claim counts."""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from usage_mirror_db import connect, initialize as initialize_usage_mirror


KEY_ID_RE = re.compile(r'^[a-zA-Z0-9_.-]{1,80}$')
PLATFORMS = {'deepseek', 'zhipu'}


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
        if key.get('platform') not in PLATFORMS:
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
