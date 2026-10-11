"""Local mirror based API usage page."""
from collections import defaultdict
from calendar import monthrange
from datetime import datetime

from flask import Blueprint, current_app, render_template, request

from auth import login_required
from usage_mirror_db import SHANGHAI, read_dashboard
from usage_sync import valid_month

usage_bp = Blueprint('usage', __name__)

PLATFORM_CARDS = (
    {
        'id': 'bailian', 'name': '百炼',
        'url': 'https://help.aliyun.com/zh/model-studio/model-pricing',
    },
    {
        'id': 'deepseek', 'name': 'DeepSeek',
        'url': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
    },
    {
        'id': 'zhipu', 'name': '智谱',
        'url': 'https://docs.bigmodel.cn/cn/coding-plan/team',
    },
    {
        'id': 'kimi', 'name': 'Kimi',
        'url': 'https://platform.kimi.com/',
    },
)


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
    cards = [{**card, 'tokens': platform_totals[card['id']],
              'has_data': card['id'] in observed_platforms}
             for card in PLATFORM_CARDS]
    key_ids = sorted({row['api_key_id'] for row in data['daily']}) if data else []
    day_values = defaultdict(dict)
    if data:
        for row in data['daily']:
            day_values[row['usage_date']][row['api_key_id']] = row['total_tokens']
    days = []
    if day_values:
        year, selected_month = map(int, month.split('-'))
        last_day = monthrange(year, selected_month)[1]
        if month == today.strftime('%Y-%m'):
            last_day = today.day
        days = [{'date': f'{month}-{day:02d}',
                 'values': day_values.get(f'{month}-{day:02d}', {}),
                 'total': sum(day_values.get(f'{month}-{day:02d}', {}).values())}
                for day in range(last_day, 0, -1)]
    last_success = data['last_success'] if data else None
    stale = True
    if last_success:
        try:
            stale = (today - datetime.fromisoformat(last_success)).total_seconds() > 7 * 3600
        except (ValueError, TypeError):
            pass
    labels = data['labels'] if data else {}
    display_labels = {key: f'{labels[key]} ({key})' if key in labels and labels[key] != key else key
                      for key in key_ids}
    return render_template('api_usage.html', page='api_usage', month=month,
                           cards=cards, key_ids=key_ids, days=days,
                           labels=display_labels,
                           latest=data['latest'] if data else None,
                           last_success=last_success, stale=stale,
                           configured=bool(options.get('enabled')))
