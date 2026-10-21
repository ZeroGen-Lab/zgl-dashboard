from flask import Flask
from config import flask_config, PORT, DINGTALK_WEBHOOK_URL, ENV, DB_PATH, DB_BAK_DIR, LLM_USAGE_SYNC
from db import ensure_tables
from routes_auth import auth_bp
from routes_dashboard import dashboard_bp
from routes_booking import booking_bp
from routes_api import api_bp
from routes_okr import okr_bp
from routes_zgantt import zgantt_bp
from routes_usage import usage_bp
from routes_agrant import agrant_bp

app = Flask(__name__)
app.config.update(flask_config)
app.secret_key = flask_config['SECRET_KEY']

ensure_tables()

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(booking_bp)
app.register_blueprint(api_bp)
app.register_blueprint(okr_bp)
app.register_blueprint(zgantt_bp)
app.register_blueprint(usage_bp)
app.register_blueprint(agrant_bp)


@app.template_filter('fmt_time')
def fmt_time(h):
    """浮点小时 -> 'HH:MM'，如 6.5 -> '06:30', 20 -> '20:00' """
    if h is None:
        return ''
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh += 1
        mm = 0
    return f"{hh:02d}:{mm:02d}"


def _onsite_report_job():
    """周一 18:30 推送 onsite 报告到钉钉群（上周 onsite + 本月达标进度，达标线=本月工作日-2）"""
    from helpers import build_onsite_report_message
    from notifier import send_dingtalk_markdown
    title, text = build_onsite_report_message()
    send_dingtalk_markdown(title, text)


def _db_backup_job():
    """每天 07:00AM 备份生产库与用量镜像库到 DB_BAK_DIR（{库名}.{日期}.bak），并清理 7 天前的备份。
    """
    import os
    import glob
    import sqlite3
    from contextlib import closing
    from datetime import datetime, timedelta
    from pathlib import Path

    sources = [DB_PATH]
    mirror = (LLM_USAGE_SYNC or {}).get('mirror_db_path')
    if mirror:
        sources.append(mirror)
    existing = [s for s in sources if os.path.exists(s)]
    for src in sources:
        if src not in existing:
            print(f"[db-backup] skipped missing {src}")

    backup_dir = DB_BAK_DIR
    os.makedirs(backup_dir, exist_ok=True)
    today_tag = datetime.now().strftime('%Y%m%d')
    for src in existing:
        dest = os.path.join(backup_dir, f"{os.path.basename(src)}.{today_tag}.bak")
        with closing(sqlite3.connect(Path(src).resolve().as_uri() + '?mode=ro', uri=True)) as s, \
                closing(sqlite3.connect(dest)) as d:
            s.backup(d)
        print(f"[db-backup] backed up {src} -> {dest}")

    # 清理超过 7 天的备份（按文件名中的执行日期判定，避免备份保留的源 mtime 干扰）
    cutoff = datetime.now() - timedelta(days=7)
    for db_name in {os.path.basename(s) for s in existing}:
        for f in glob.glob(os.path.join(backup_dir, f"{db_name}.*.bak")):
            fname = os.path.basename(f)
            tag = fname[len(db_name) + 1:-len('.bak')]
            try:
                file_date = datetime.strptime(tag, '%Y%m%d')
            except ValueError:
                continue  # 非本规则命名的文件，跳过不动
            if file_date < cutoff:
                os.remove(f)
                print(f"[db-backup] removed expired {f}")


def _usage_sync_job():
    """从 Proxy 的只读接口更新独立的 API 用量镜像库。"""
    from usage_sync import SyncError, sync_usage
    try:
        result = sync_usage(LLM_USAGE_SYNC, DB_PATH)
        app.logger.info('API 用量同步完成：%s', result)
    except Exception as exc:
        app.logger.error('API 用量同步失败：%s', exc if isinstance(exc, (SyncError, ValueError)) else type(exc).__name__)


# 备份任务仅在生产环境注册（pre 环境的数据不备份，以免覆盖生产备份）
_need_backup = ENV == 'prod'
_need_usage = ENV in ('prod', 'pre') and bool((LLM_USAGE_SYNC or {}).get('enabled'))
_need_onsite_report = ENV == 'prod' and bool(DINGTALK_WEBHOOK_URL)
if _need_backup or _need_onsite_report or _need_usage:
    from datetime import datetime, timedelta, timezone
    from apscheduler.schedulers.background import BackgroundScheduler
    _shanghai_tz = timezone(timedelta(hours=8))
    _scheduler = BackgroundScheduler()
    if _need_backup:
        _scheduler.add_job(_db_backup_job, 'cron', hour=7, minute=0)
    if _need_onsite_report:
        _scheduler.add_job(_onsite_report_job, 'cron', day_of_week='mon', hour=18, minute=30)
    if _need_usage:
        _scheduler.add_job(_usage_sync_job, 'cron', hour='0,3,6,9,12,15,18,21', minute=0,
                           timezone=_shanghai_tz,
                           id='usage_sync', coalesce=True, max_instances=1)
        # 启动后补一次同步，页面无需等待下一个三小时整点。
        _scheduler.add_job(_usage_sync_job, 'date',
                           run_date=datetime.now(_shanghai_tz) + timedelta(seconds=2),
                           id='usage_sync_startup')
    _scheduler.start()

if __name__ == '__main__':
    app.run(host='127.0.0.1' if ENV == 'test' else '0.0.0.0', port=PORT, threaded=True)
