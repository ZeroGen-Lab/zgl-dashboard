"""Fetch Proxy's read-only usage API into the independent local mirror."""
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

import requests

from usage_mirror_db import connect, initialize, timestamp

LOG = logging.getLogger(__name__)
MONTH_RE = re.compile(r'^[0-9]{4}-(0[1-9]|1[0-2])$')
RETRYABLE = {429, 502, 503, 504}


class SyncError(Exception):
    pass


def valid_month(value):
    return isinstance(value, str) and bool(MONTH_RE.fullmatch(value)) and 1 <= int(value[:4]) <= 9998


def months_between(start, end):
    year, month = map(int, start.split('-'))
    while (year, month) <= tuple(map(int, end.split('-'))):
        yield f'{year:04d}-{month:02d}'
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def previous_month(value, count):
    year, month = map(int, value.split('-'))
    index = year * 12 + month - 1 - count
    if index < 12:
        return '0001-01'
    year, zero_month = divmod(index, 12)
    return f'{year:04d}-{zero_month + 1:02d}'


def text_field(value, field):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise SyncError(f'{field} 字段无效')
    return value


def integer_field(value, field):
    if type(value) is not int or value < 0:
        raise SyncError(f'{field} 字段无效')
    return value


def time_field(value, field):
    text_field(value, field)
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise SyncError(f'{field} 时间格式无效') from exc
    return value


def fetch(session, base_url, token, path, params, timeout):
    url = base_url.rstrip('/') + path
    for attempt in range(3):
        try:
            response = session.get(url, params=params,
                                   headers={'Authorization': 'Bearer ' + token},
                                   timeout=(min(timeout, 10), timeout), allow_redirects=False)
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise SyncError('Proxy 连接失败') from exc
        try:
            if response.status_code in RETRYABLE and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise SyncError(f'Proxy HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError as exc:
                raise SyncError('Proxy 返回了无效 JSON') from exc
            if not isinstance(data, dict):
                raise SyncError('Proxy 返回结构无效')
            return data
        finally:
            response.close()


def fetch_summary(session, base_url, token, month, timeout):
    data = fetch(session, base_url, token, '/internal/usage', {'month': month}, timeout)
    version = data.get('version')
    if version not in (1, 2) or data.get('month') != month or data.get('timezone') != 'Asia/Shanghai':
        raise SyncError('Proxy 汇总版本、月份或时区不匹配')
    generated = time_field(data.get('generated_at'), 'generated_at')
    if not isinstance(data.get('rows'), list) or not isinstance(data.get('keys'), list):
        raise SyncError('Proxy 汇总缺少 rows 或 keys')
    rows = []
    for source in data['rows']:
        if not isinstance(source, dict) or source.get('month_key') != month:
            raise SyncError('Proxy 汇总行月份不匹配')
        rows.append((month, *(text_field(source.get(f), f) for f in ('platform', 'api_key_id', 'owner_snapshot')),
                     *(integer_field(source.get(f), f) for f in ('input_tokens', 'output_tokens', 'total_tokens',
                                                                'reasoning_tokens', 'success_calls', 'error_calls')),
                     time_field(source.get('updated_at'), 'updated_at')))
    if version == 2:
        if not isinstance(data.get('member_rows'), list) or not isinstance(data.get('key_rows'), list):
            raise SyncError('Proxy 汇总缺少人员或 Key 维度')
        member_rows = parse_dimension_rows(data['member_rows'], month, 'owner_snapshot')
        key_rows = parse_dimension_rows(data['key_rows'], month, 'api_key_id')
    else:
        member_rows = aggregate_legacy_rows(data['rows'], month, 'owner_snapshot')
        key_rows = aggregate_legacy_rows(data['rows'], month, 'api_key_id')
    keys = []
    for source in data['keys']:
        if not isinstance(source, dict) or type(source.get('enabled')) is not bool:
            raise SyncError('Proxy Key 元数据无效')
        keys.append((text_field(source.get('id'), 'id'), text_field(source.get('platform'), 'platform'),
                     text_field(source.get('owner'), 'owner'), text_field(source.get('label'), 'label'),
                     int(source['enabled'])))
    return rows, member_rows, key_rows, keys, generated


def parse_dimension_rows(sources, month, dimension):
    rows = []
    for source in sources:
        if not isinstance(source, dict) or source.get('month_key') != month:
            raise SyncError('Proxy 分维度汇总行月份不匹配')
        rows.append((month, text_field(source.get('platform'), 'platform'),
                     text_field(source.get(dimension), dimension), text_field(source.get('model'), 'model'),
                     *(integer_field(source.get(field), field) for field in (
                         'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens',
                         'success_calls', 'error_calls')),
                     time_field(source.get('updated_at'), 'updated_at')))
    return rows


def aggregate_legacy_rows(sources, month, dimension):
    """Keep v1 Proxy compatibility; its monthly rows did not retain model names."""
    totals = {}
    for source in sources:
        if not isinstance(source, dict) or source.get('month_key') != month:
            raise SyncError('Proxy 汇总行月份不匹配')
        key = (text_field(source.get('platform'), 'platform'),
               text_field(source.get(dimension), dimension))
        metrics = [integer_field(source.get(field), field) for field in (
            'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens',
            'success_calls', 'error_calls')]
        updated = time_field(source.get('updated_at'), 'updated_at')
        current = totals.setdefault(key, [0] * 6 + [updated])
        for index, value in enumerate(metrics):
            current[index] += value
        current[6] = max(current[6], updated)
    return [(month, platform, value, '历史未细分', *metrics)
            for (platform, value), metrics in totals.items()]


def model_summaries_from_records(month, summaries, records):
    """Use real models only when detail matches every monthly source row exactly."""
    if not records:
        return None
    expected = defaultdict(lambda: [0] * 6)
    actual = defaultdict(lambda: [0] * 6)
    members = defaultdict(lambda: [0] * 6)
    keys = defaultdict(lambda: [0] * 6)
    for row in summaries:
        metrics = expected[(row[1], row[2], row[3])]
        for index, value in enumerate(row[4:10]):
            metrics[index] += value
    for row in records:
        if row[7] == 'pending':
            continue
        values = (*row[8:12], int(row[7] == 'success'), int(row[7] == 'error'))
        for bucket in (actual[(row[3], row[4], row[5])],
                       members[(row[3], row[5], row[6])],
                       keys[(row[3], row[4], row[6])]):
            for index, value in enumerate(values):
                bucket[index] += value
    if dict(expected) != dict(actual):
        return None
    updated = timestamp()
    member_rows = [(month, platform, owner, model, *metrics, updated)
                   for (platform, owner, model), metrics in sorted(members.items())]
    key_rows = [(month, platform, key_id, model, *metrics, updated)
                for (platform, key_id, model), metrics in sorted(keys.items())]
    return member_rows, key_rows


def fetch_records(session, base_url, token, month, timeout):
    after = 0
    records = []
    seen = set()
    while True:
        data = fetch(session, base_url, token, '/internal/usage/records',
                     {'month': month, 'after': after, 'limit': 500}, timeout)
        if not isinstance(data.get('records'), list) or len(data['records']) > 500:
            raise SyncError('Proxy 明细分页结构无效')
        previous = after
        for source in data['records']:
            if not isinstance(source, dict):
                raise SyncError('Proxy 明细行无效')
            remote_id = integer_field(source.get('id'), 'id')
            request_id = text_field(source.get('request_id'), 'request_id')
            if remote_id <= after or request_id in seen:
                raise SyncError('Proxy 明细分页顺序无效')
            seen.add(request_id)
            requested_at = time_field(source.get('requested_at'), 'requested_at')
            if requested_at[:7] != month:
                raise SyncError('Proxy 明细月份不匹配')
            status = source.get('status')
            if status not in ('pending', 'success', 'error'):
                raise SyncError('Proxy 明细状态无效')
            records.append((request_id, remote_id,
                            *(text_field(source.get(f), f) for f in ('session_id', 'platform', 'api_key_id',
                                                                      'owner_snapshot', 'model')),
                            status, *(integer_field(source.get(f), f) for f in ('input_tokens', 'output_tokens',
                                                                                'total_tokens', 'reasoning_tokens')),
                            requested_at))
            after = remote_id
        next_after = data.get('next_after')
        if next_after is None:
            break
        if type(next_after) is not int or next_after != after or after <= previous:
            raise SyncError('Proxy 明细分页游标无效')
    return records


@contextmanager
def process_lock(path):
    """Prevent a manual sync from overlapping the scheduled sync."""
    with open(str(path) + '.sync.lock', 'a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.seek(0)
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError('已有用量同步任务正在运行') from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sync_usage(config, primary_path, session=None, now=None):
    if not config.get('enabled'):
        raise SyncError('LLM 用量同步未启用')
    base_url = config.get('base_url') or ''
    token = config.get('dashboard_token', '')
    path = config.get('mirror_db_path')
    if not isinstance(base_url, str) or not base_url.startswith(('https://', 'http://')) or not token or not path:
        raise SyncError('LLM 用量同步配置不完整')
    base_url = base_url.rstrip('/')
    # if base_url.startswith('http://'):
    #     LOG.warning('Proxy 使用 HTTP，服务令牌会在传输中暴露；建议改为 HTTPS')
    timeout = config.get('timeout_seconds', 20)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise SyncError('LLM 用量超时配置无效')
    current = (now or datetime.now(timezone(timedelta(hours=8)))).strftime('%Y-%m')
    start = config.get('sync_start_month', current)
    if not valid_month(start) or start > current:
        raise SyncError('同步起始月份无效')
    initialize(path, primary_path)
    months = list(months_between(start, current))
    detail_start = max(start, previous_month(current, 2))
    own_session = session is None
    session = session or requests.Session()
    errors = []
    summary_count = record_count = 0
    with process_lock(path):
        run_id = None
        try:
            with closing(connect(path)) as conn, conn:
                run_id = conn.execute("INSERT INTO usage_sync_runs(started_at,status) VALUES (?,'running')",
                                      (timestamp(),)).lastrowid
            for month in months:
                try:
                    summaries, member_summaries, key_summaries, keys, generated = fetch_summary(
                        session, base_url, token, month, timeout)
                    records = fetch_records(session, base_url, token, month, timeout) if month >= detail_start else []
                    if any(row[3] == '历史未细分' for row in member_summaries + key_summaries):
                        detailed = model_summaries_from_records(month, summaries, records)
                        if detailed is not None:
                            member_summaries, key_summaries = detailed
                    mirrored = timestamp()
                    with closing(connect(path)) as conn, conn:
                        conn.execute('DELETE FROM usage_monthly WHERE month_key=?', (month,))
                        conn.execute('DELETE FROM usage_monthly_by_member WHERE month_key=?', (month,))
                        conn.execute('DELETE FROM usage_monthly_by_key WHERE month_key=?', (month,))
                        conn.executemany('''INSERT INTO usage_monthly VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(month_key,platform,api_key_id,owner_snapshot) DO UPDATE SET
                            input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
                            total_tokens=excluded.total_tokens,reasoning_tokens=excluded.reasoning_tokens,
                            success_calls=excluded.success_calls,error_calls=excluded.error_calls,
                            source_updated_at=excluded.source_updated_at,mirrored_at=excluded.mirrored_at''',
                                         [(*r, mirrored) for r in summaries])
                        conn.executemany('INSERT INTO usage_monthly_by_member VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                         [(*r, mirrored) for r in member_summaries])
                        conn.executemany('INSERT INTO usage_monthly_by_key VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                         [(*r, mirrored) for r in key_summaries])
                        conn.executemany('''INSERT INTO usage_key_metadata VALUES (?,?,?,?,?,?)
                            ON CONFLICT(api_key_id) DO UPDATE SET platform=excluded.platform,
                            owner=excluded.owner,label=excluded.label,enabled=excluded.enabled,
                            mirrored_at=excluded.mirrored_at''', [(*r, mirrored) for r in keys])
                        conn.executemany('''INSERT INTO usage_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(request_id) DO UPDATE SET remote_id=excluded.remote_id,
                            session_id=excluded.session_id,platform=excluded.platform,
                            api_key_id=excluded.api_key_id,owner_snapshot=excluded.owner_snapshot,
                            model=excluded.model,status=excluded.status,input_tokens=excluded.input_tokens,
                            output_tokens=excluded.output_tokens,total_tokens=excluded.total_tokens,
                            reasoning_tokens=excluded.reasoning_tokens,requested_at=excluded.requested_at,
                            mirrored_at=excluded.mirrored_at''', [(*r, mirrored) for r in records])
                        conn.execute('UPDATE usage_sync_runs SET source_generated_at=? WHERE id=?',
                                     (generated, run_id))
                    summary_count += len(summaries)
                    record_count += len(records)
                    LOG.info('月份 %s 同步完成：汇总 %d 行，明细 %d 行', month, len(summaries), len(records))
                except (SyncError, requests.RequestException, sqlite3.Error, OSError) as exc:
                    errors.append(month)
                    LOG.warning('月份 %s 同步失败：%s', month,
                                str(exc) if isinstance(exc, SyncError) else '数据库或网络错误')
            status = 'success' if not errors else ('partial' if len(errors) < len(months) else 'error')
            with closing(connect(path)) as conn, conn:
                conn.execute('''UPDATE usage_sync_runs SET finished_at=?,status=?,months_requested=?,
                    records_upserted=?,summaries_upserted=?,error_message=? WHERE id=?''',
                             (timestamp(), status, len(months), record_count, summary_count,
                              ('失败月份：' + ', '.join(errors))[:300] if errors else '', run_id))
            if errors:
                raise SyncError('部分月份同步失败：' + ', '.join(errors))
            return {'months': len(months), 'records': record_count, 'summaries': summary_count}
        except Exception:
            if run_id is not None:
                with closing(connect(path)) as conn, conn:
                    conn.execute('''UPDATE usage_sync_runs SET finished_at=?,status='error',
                        error_message='同步异常中断' WHERE id=? AND status='running' ''',
                                 (timestamp(), run_id))
            raise
        finally:
            if own_session:
                session.close()
