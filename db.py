import sqlite3
from config import DB_PATH


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    """确保所有必要的表存在，启动时调用"""
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS sign_ins
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, timestamp DATETIME)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS users
                    (uid TEXT PRIMARY KEY, name TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_plans
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL,
                     week_key TEXT NOT NULL,
                     content TEXT NOT NULL,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(uid, week_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_completions
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL,
                     date TEXT NOT NULL,
                     content TEXT NOT NULL,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(uid, date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS booking_slots
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     publisher TEXT NOT NULL,
                     slot_type TEXT NOT NULL CHECK(slot_type IN ('recurring', 'one_time')),
                     title TEXT NOT NULL,
                     description TEXT DEFAULT '',
                     day_of_week INTEGER,
                     start_hour INTEGER NOT NULL,
                     end_hour INTEGER NOT NULL,
                     specific_date TEXT,
                     capacity INTEGER DEFAULT 1,
                     status TEXT DEFAULT 'active' CHECK(status IN ('active', 'cancelled')),
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS bookings
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     slot_id INTEGER NOT NULL,
                     booker TEXT NOT NULL,
                     instance_date TEXT NOT NULL,
                     status TEXT DEFAULT 'active' CHECK(status IN ('active', 'cancelled')),
                     booked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(slot_id, booker, instance_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS monthly_summaries
                    (uid TEXT NOT NULL,
                     month_key TEXT NOT NULL,
                     summary TEXT,
                     suggestion TEXT,
                     generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(uid, month_key))''')
    conn.commit()
    conn.close()