"""Optional manual one-off LLM usage sync for checking connectivity and data."""
import logging
import sys

from config import DB_PATH, LLM_USAGE_SYNC
from usage_sync import SyncError, sync_usage


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        result = sync_usage(LLM_USAGE_SYNC, DB_PATH)
    except (SyncError, OSError, ValueError) as exc:
        logging.error('LLM 用量同步失败：%s', exc)
        return 1
    logging.info('LLM 用量同步成功：%d 个月，%d 条明细，%d 条月汇总，清理 %d 条过期明细',
                 result['months'], result['records'], result['summaries'],
                 result.get('records_deleted', 0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
