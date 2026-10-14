"""Local mirror based API usage page."""
import hmac
import logging
import secrets
import sqlite3
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, session
import yaml

from auth import is_admin, login_required
from config import change_claim, claim_status, masked
from db_usage import SHANGHAI, read_dashboard, read_member_usage
from usage_sync import SyncError, sync_usage, valid_month

usage_bp = Blueprint('usage', __name__)
LOG = logging.getLogger(__name__)
INFO_PATH = Path(__file__).with_name('platform_models.yml')

PROXY_PUBLIC_BASE_URL = 'http://120.55.190.84'

PLATFORM_CARDS = (
    {'id': 'zhipu', 'name': '智谱'},
    {'id': 'deepseek', 'name': 'DeepSeek'},
    {'id': 'bailian', 'name': '百炼'},
    {'id': 'kimi', 'name': 'Kimi'},
)


def load_platform_info(path=INFO_PATH):
    """Read on each page request so YAML edits take effect on refresh."""
    try:
        with open(path, encoding='utf-8') as source:
            data = yaml.safe_load(source) or {}
    except (OSError, yaml.YAMLError) as exc:
        LOG.warning('无法读取平台模型介绍：%s', exc)
        return {}
    platforms = data.get('platforms') if isinstance(data, dict) else None
    if not isinstance(platforms, dict):
        LOG.warning('平台模型介绍缺少 platforms 映射')
        return {}
    result = {}
    for platform, info in platforms.items():
        if not isinstance(platform, str) or not isinstance(info, dict):
            continue
        title = info.get('title', '')
        lines = info.get('lines', [])
        note = info.get('note', '')
        url = info.get('pricing_url', '')
        if (not isinstance(title, str) or not isinstance(lines, list) or
                not all(isinstance(line, str) for line in lines) or
                not isinstance(note, str) or not isinstance(url, str)):
            LOG.warning('平台 %s 的模型介绍字段格式无效', platform)
            continue
        result[platform] = {'title': title, 'lines': lines, 'note': note,
                            'pricing_url': url if url.startswith(('https://', 'http://')) else ''}
    return result


def model_chart(series, dates):
    """Prepare seven stacked daily bars, one segment per model."""
    return {
        'labels': [date[5:] for date in reversed(dates)],
        'models': [
            {'name': model, 'values': [values.get(date, 0) for date in reversed(dates)]}
            for model, values in sorted(series.items())
        ],
    }


@usage_bp.route('/api-usage')
@login_required
def api_usage():
    today = datetime.now(SHANGHAI)
    month = request.args.get('month') or today.strftime('%Y-%m')
    if not valid_month(month):
        month = today.strftime('%Y-%m')
    options = current_app.config.get('LLM_USAGE_SYNC') or {}
    data = read_dashboard(options.get('mirror_db_path'), month)
    platforms = data['platforms'] if data else []
    platform_totals = defaultdict(int)
    observed_platforms = set()
    for row in platforms:
        platform = row['platform'].lower()
        platform = 'kimi' if platform == 'moonshot' else platform
        platform_totals[platform] += row['total_tokens']
        observed_platforms.add(platform)
    coding_keys = current_app.config.get('CODING_PLAN_KEYS') or []
    counts, claimed = claim_status(options.get('mirror_db_path'), session['user']) if coding_keys else ({}, set())
    platform_info = load_platform_info()
    cards = [{**card, 'tokens': platform_totals[card['id']],
              'info': platform_info.get(card['id'], {}),
              'base_url': f'{PROXY_PUBLIC_BASE_URL}/api/llm/{card["id"]}/v1'
              if card['id'] != 'kimi' else None,
              'coding_keys': [
                  {'id': key['id'], 'label': key['label'],
                   'masked': masked(key['secret']) if key['secret'] else '待填写 Key',
                   'configured': bool(key['secret']),
                   'count': counts.get(key['id'], 0), 'claimed': key['id'] in claimed}
                  for key in coding_keys if key['platform'] == card['id']
              ],
              'has_data': card['id'] in observed_platforms}
             for card in PLATFORM_CARDS]
    year, month_number = map(int, month.split('-'))
    chart_end = (today.date() if month == today.strftime('%Y-%m') else
                 date(year, month_number, monthrange(year, month_number)[1]))
    recent_dates = [(chart_end - timedelta(days=offset)).isoformat() for offset in range(7)]
    member_data = read_member_usage(options.get('mirror_db_path'), month,
                                    max(recent_dates[-1], month + '-01'),
                                    (chart_end + timedelta(days=1)).isoformat())
    platform_models = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    member_models = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for row in member_data['daily_models'] if member_data else []:
        platform = row['platform'].lower()
        platform = 'kimi' if platform == 'moonshot' else platform
        platform_models[platform][row['model']][row['usage_date']] += row['total_tokens']
        member_models[row['owner_snapshot']][f'{platform} · {row["model"]}'][row['usage_date']] += row['total_tokens']
    members = [{'name': row['owner_snapshot'], 'total_tokens': row['total_tokens'],
                'chart_id': f'member-{index}'}
               for index, row in enumerate(member_data['monthly'])] if member_data else []
    model_charts = {f'platform-{card["id"]}': model_chart(platform_models[card['id']], recent_dates)
                    for card in cards}
    model_charts.update({member['chart_id']: model_chart(member_models[member['name']], recent_dates)
                         for member in members})
    last_success = data['last_success'] if data else None
    stale = True
    if last_success:
        try:
            stale = (today - datetime.fromisoformat(last_success)).total_seconds() > 7 * 3600
        except (ValueError, TypeError):
            pass
    csrf = session.get('coding_plan_csrf')
    if not csrf:
        csrf = secrets.token_urlsafe(32)
        session['coding_plan_csrf'] = csrf
    sync_csrf = session.get('usage_sync_csrf')
    if is_admin() and not sync_csrf:
        sync_csrf = secrets.token_urlsafe(32)
        session['usage_sync_csrf'] = sync_csrf
    return render_template('api_usage.html', page='api_usage', month=month,
                           cards=cards, members=members,
                           is_current_month=month == today.strftime('%Y-%m'),
                           proxy_public_base_url=PROXY_PUBLIC_BASE_URL,
                           recent_dates=recent_dates, model_charts=model_charts,
                           coding_plan_csrf=csrf, usage_sync_csrf=sync_csrf,
                           is_admin=is_admin(),
                           latest=data['latest'] if data else None,
                           last_success=last_success, stale=stale,
                           configured=bool(options.get('enabled')))


@usage_bp.post('/api-usage/sync')
def sync_api_usage():
    if not session.get('user'):
        return jsonify(error='请先登录'), 401
    if not is_admin():
        return jsonify(error='仅管理员可以更新用量'), 403
    csrf = request.headers.get('X-CSRF-Token', '')
    expected = session.get('usage_sync_csrf', '')
    if not csrf or not expected or not hmac.compare_digest(csrf, expected):
        return jsonify(error='页面已过期，请刷新后重试'), 403
    options = current_app.config.get('LLM_USAGE_SYNC') or {}
    if not options.get('enabled'):
        return jsonify(error='用量同步尚未启用，请先检查配置'), 503
    try:
        result = sync_usage(options, current_app.config['DB_PATH'])
    except SyncError as exc:
        current_app.logger.warning('管理员手动同步 API 用量失败：%s', exc)
        return jsonify(error=str(exc)), 409
    except Exception:
        current_app.logger.exception('管理员手动同步 API 用量失败')
        return jsonify(error='同步失败，请查看服务日志'), 500
    return jsonify(message='用量已更新', **result)


@usage_bp.post('/api-usage/coding-keys/<key_id>/<action>')
def coding_plan_key_action(key_id, action):
    if not session.get('user'):
        return jsonify(error='请先登录'), 401
    csrf = request.headers.get('X-CSRF-Token', '')
    expected = session.get('coding_plan_csrf', '')
    if not csrf or not expected or not hmac.compare_digest(csrf, expected):
        return jsonify(error='页面已过期，请刷新后重试'), 403
    if action not in ('claim', 'reveal', 'release'):
        return jsonify(error='无效操作'), 404
    key = next((item for item in current_app.config.get('CODING_PLAN_KEYS', [])
                if item['id'] == key_id), None)
    if key is None:
        return jsonify(error='找不到这个 Key'), 404
    if not key['secret']:
        return jsonify(error='这个 Key 尚未配置'), 409
    path = (current_app.config.get('LLM_USAGE_SYNC') or {}).get('mirror_db_path')
    if not path:
        return jsonify(error='用量数据库路径尚未配置'), 503
    try:
        if action == 'reveal':
            counts, claimed = claim_status(path, session['user'])
            if key_id not in claimed:
                return jsonify(error='请先登记使用'), 403
            count = counts.get(key_id, 0)
            is_claimed = True
        else:
            count, is_claimed = change_claim(path, current_app.config['DB_PATH'], key_id,
                                             session['user'], action)
    except (OSError, sqlite3.Error, ValueError):
        current_app.logger.exception('Coding Plan 使用登记失败')
        return jsonify(error='暂时无法保存使用登记'), 500
    payload = {'count': count, 'claimed': is_claimed}
    if action in ('claim', 'reveal'):
        payload['secret'] = key['secret']
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response
