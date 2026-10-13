"""Editable model descriptions for the API usage platform cards."""
import logging
from pathlib import Path

import yaml


LOG = logging.getLogger(__name__)
INFO_PATH = Path(__file__).with_name('platform_models.yml')


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
