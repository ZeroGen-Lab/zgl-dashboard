from flask import Flask
from config import flask_config, PORT, DINGTALK_WEBHOOK_URL
from db import ensure_tables
from routes_auth import auth_bp
from routes_dashboard import dashboard_bp
from routes_booking import booking_bp
from routes_api import api_bp

app = Flask(__name__)
app.config.update(flask_config)
app.secret_key = flask_config['SECRET_KEY']

ensure_tables()

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(booking_bp)
app.register_blueprint(api_bp)


def _weekly_summary_job():
    """周一 12:30 自动推送周报到钉钉群"""
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


if DINGTALK_WEBHOOK_URL:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_weekly_summary_job, 'cron', day_of_week='mon', hour=12, minute=30)
    _scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)