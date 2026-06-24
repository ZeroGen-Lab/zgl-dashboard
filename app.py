from flask import Flask
from config import flask_config, PORT, DINGTALK_WEBHOOK_URL, ENV, DB_PATH, DB_BAK_DIR
from db import ensure_tables
from routes_auth import auth_bp
from routes_dashboard import dashboard_bp
from routes_booking import booking_bp
from routes_api import api_bp
from routes_okr import okr_bp

app = Flask(__name__)
app.config.update(flask_config)
app.secret_key = flask_config['SECRET_KEY']

ensure_tables()

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(booking_bp)
app.register_blueprint(api_bp)
app.register_blueprint(okr_bp)


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


def _weekly_summary_job():
    """周一 18:30 自动推送周报到钉钉群"""
    from helpers import compute_summary_week_range, generate_weekly_summary
    from notifier import send_dingtalk_markdown
    summary_monday, summary_sunday, _, _ = compute_summary_week_range(0)
    date_range = f"{summary_monday.strftime('%m/%d')} - {summary_sunday.strftime('%m/%d')}"
    summary_list = generate_weekly_summary(0)
    lines = [f"## ZGL Dashboard Report {date_range}", "",
             "Name | Onsite Days | Daily Comp | Weekly Plan", ":---:|:---:|:---:|:---:"]
    for s in summary_list:
        plan_icon = "✅" if s['has_weekly_plan'] else "NA"
        lines.append(f"{s['name']} | {s['onsite_days']}d | {s['completion_count']} | {plan_icon}")
    send_dingtalk_markdown(f"ZGL Dashboard Report {date_range}", "\n".join(lines))


def _db_backup_job():
    """每天 06:00AM 将生产库备份，以执行日期命名（{DB_PATH}.YYYYMMDD.bak），并清理 7 天前的备份。"""
    import os
    import shutil
    import glob
    from datetime import datetime, timedelta

    backup_dir = DB_BAK_DIR
    db_name = os.path.basename(DB_PATH)  # attendance.db
    today_tag = datetime.now().strftime('%Y%m%d')
    dest = os.path.join(backup_dir, f"{db_name}.{today_tag}.bak")

    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(DB_PATH, dest)
    print(f"[db-backup] copied {DB_PATH} -> {dest}")

    # 清理超过 7 天的备份（按文件名中的执行日期判定，避免 copy2 保留的源 mtime 干扰）
    cutoff = datetime.now() - timedelta(days=7)
    prefix, suffix = f"{db_name}.", ".bak"
    for f in glob.glob(os.path.join(backup_dir, f"{db_name}.*.bak")):
        fname = os.path.basename(f)
        tag = fname[len(prefix):-len(suffix)]
        try:
            file_date = datetime.strptime(tag, '%Y%m%d')
        except ValueError:
            continue  # 非本规则命名的文件，跳过不动
        if file_date < cutoff:
            os.remove(f)
            print(f"[db-backup] removed expired {f}")


# 备份任务仅在生产环境注册（pre 环境的数据不备份，以免覆盖生产备份）
_need_backup = ENV == 'prod'
_need_weekly = bool(DINGTALK_WEBHOOK_URL)
if _need_backup or _need_weekly:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    if _need_backup:
        _scheduler.add_job(_db_backup_job, 'cron', hour=6, minute=0)
    if _need_weekly:
        _scheduler.add_job(_weekly_summary_job, 'cron', day_of_week='mon', hour=18, minute=30)
    _scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)