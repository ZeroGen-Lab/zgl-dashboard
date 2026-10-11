"""Independent, read-only-to-the-web mirror of Proxy usage statistics."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))


def timestamp():
    return datetime.now(SHANGHAI).isoformat(timespec='seconds')


def validate_path(mirror_path, primary_path):
    if not mirror_path:
        raise ValueError('LLM 用量镜像数据库路径未配置')
    if not Path(mirror_path).is_absolute():
        raise ValueError('LLM 用量镜像数据库必须使用绝对路径')
    if Path(mirror_path).resolve() == Path(primary_path).resolve():
        raise ValueError('LLM 用量镜像数据库不能与主业务数据库共用路径')


def connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    else:
        conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def initialize(path, primary_path):
    validate_path(path, primary_path)
    Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as conn:
        conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS usage_records (
                request_id TEXT PRIMARY KEY,
                remote_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
                owner_snapshot TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','success','error')),
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                requested_at TEXT NOT NULL,
                mirrored_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_records_time ON usage_records(requested_at);
            CREATE INDEX IF NOT EXISTS idx_usage_records_key_time ON usage_records(api_key_id, requested_at);
            CREATE TABLE IF NOT EXISTS usage_monthly (
                month_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                api_key_id TEXT NOT NULL,
                owner_snapshot TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                success_calls INTEGER NOT NULL DEFAULT 0,
                error_calls INTEGER NOT NULL DEFAULT 0,
                source_updated_at TEXT NOT NULL,
                mirrored_at TEXT NOT NULL,
                PRIMARY KEY(month_key, platform, api_key_id, owner_snapshot)
            );
            CREATE TABLE IF NOT EXISTS usage_key_metadata (
                api_key_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                owner TEXT NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                mirrored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('running','success','partial','error')),
                months_requested INTEGER NOT NULL DEFAULT 0,
                records_upserted INTEGER NOT NULL DEFAULT 0,
                summaries_upserted INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                source_generated_at TEXT
            );
        ''')
    if os.name != 'nt':
        os.chmod(path, 0o600)


def read_dashboard(path, month):
    """Return local aggregates without creating a missing mirror file."""
    if not path or not Path(path).is_file():
        return None
    try:
        with closing(connect(path, readonly=True)) as conn:
            latest = conn.execute('SELECT * FROM usage_sync_runs ORDER BY id DESC LIMIT 1').fetchone()
            success = conn.execute("SELECT finished_at FROM usage_sync_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
            months = [r[0] for r in conn.execute('SELECT DISTINCT month_key FROM usage_monthly ORDER BY month_key DESC')]
            platforms = [dict(r) for r in conn.execute('''
                SELECT platform, SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
                       SUM(total_tokens) total_tokens, SUM(success_calls) success_calls,
                       SUM(error_calls) error_calls
                FROM usage_monthly WHERE month_key=? GROUP BY platform ORDER BY platform
            ''', (month,))]
            start = month + '-01'
            year, part = map(int, month.split('-'))
            end = f'{year + 1:04d}-01-01' if part == 12 else f'{year:04d}-{part + 1:02d}-01'
            daily = [dict(r) for r in conn.execute('''
                SELECT substr(requested_at,1,10) usage_date, api_key_id, SUM(total_tokens) total_tokens
                FROM usage_records WHERE requested_at>=? AND requested_at<?
                  AND status IN ('success','error')
                GROUP BY usage_date, api_key_id ORDER BY usage_date DESC, api_key_id
            ''', (start, end))]
            labels = {r['api_key_id']: r['label'] for r in conn.execute('SELECT api_key_id,label FROM usage_key_metadata')}
            return {'latest': dict(latest) if latest else None,
                    'last_success': success[0] if success else None,
                    'months': months, 'platforms': platforms, 'daily': daily, 'labels': labels}
    except sqlite3.Error:
        return None
