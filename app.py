from flask import Flask
from config import flask_config, PORT
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)