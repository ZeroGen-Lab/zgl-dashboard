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
    # loginname：登录用户名（来自 .users.txt），与 IC 卡 uid 1:1 绑定；
    # SQLite 的 UNIQUE 把 NULL 视为互异，故允许多个 NULL（未绑定的卡），非 NULL 登录名唯一
    conn.execute('''CREATE TABLE IF NOT EXISTS users
                    (uid TEXT PRIMARY KEY, name TEXT, loginname TEXT UNIQUE)''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_plans
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL,
                     week_key TEXT NOT NULL,
                     content TEXT NOT NULL,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(uid, week_key))''')
    # weekly plan 的 TODO 列表（itemized）；status 供后续 daily completion 标记完成（done）
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_plan_items
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL,
                     week_key TEXT NOT NULL,
                     item_order INTEGER NOT NULL,
                     text TEXT NOT NULL,
                     status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
                     completed_date TEXT,
                     completed_at DATETIME,
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wpi_uid_week "
                 "ON weekly_plan_items(uid, week_key)")
    
    # review：上一工作周期总结（必填）；todo：下一工作周期计划（可选）
    # 旧库的 content 列已通过迁移脚本改为 review（旧值即 review），todo 回填为 NULL
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_completions
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL,
                     date TEXT NOT NULL,
                     review TEXT NOT NULL,
                     todo TEXT,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(uid, date))''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS booking_slots
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     publisher TEXT NOT NULL,
                     slot_type TEXT NOT NULL CHECK(slot_type IN ('recurring', 'one_time')),
                     title TEXT NOT NULL,
                     description TEXT DEFAULT '',
                     day_of_week INTEGER,
                     start_hour REAL NOT NULL,
                     end_hour REAL NOT NULL,
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
   
    conn.execute('''CREATE TABLE IF NOT EXISTS okr_cycles
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     cycle_key TEXT NOT NULL UNIQUE,
                     status TEXT NOT NULL DEFAULT 'brainstorming'
                        CHECK(status IN ('brainstorming','planning','active','closed')),
                     created_by TEXT NOT NULL,
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS okr_objectives
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     cycle_id INTEGER NOT NULL REFERENCES okr_cycles(id),
                     title TEXT NOT NULL,
                     description TEXT DEFAULT '',
                     status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','approved','rejected')),
                     proposed_by TEXT NOT NULL,
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS okr_key_results
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     objective_id INTEGER NOT NULL REFERENCES okr_objectives(id),
                     uid TEXT NOT NULL,
                     title TEXT NOT NULL,
                     description TEXT DEFAULT '',
                     progress INTEGER NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 100),
                     status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','pending_edit')),
                     pending_title TEXT DEFAULT '',
                     pending_description TEXT DEFAULT '',
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS okr_kr_milestones
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     kr_id INTEGER NOT NULL REFERENCES okr_key_results(id),
                     description TEXT NOT NULL,
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    # LLM 请求/响应追踪：每次 llm.call_deepseek 调用落库一行
    # request_id 唯一标识单次请求；session_id 把同一会话(多轮)的请求归到一组，建索引便于按 id 取回
    # prompt/token_usage 存 JSON 字符串(TEXT)；requested_at 为请求发出时刻
    conn.execute('''CREATE TABLE IF NOT EXISTS llm_calls
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     request_id TEXT NOT NULL UNIQUE,
                     session_id TEXT NOT NULL,
                     uid TEXT,
                     model TEXT,
                     prompt TEXT,
                     response TEXT,
                     status TEXT NOT NULL DEFAULT 'success' CHECK(status IN ('success','error')),
                     token_usage TEXT,
                     error TEXT,
                     requested_at DATETIME)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")

    conn.commit()
    conn.close()