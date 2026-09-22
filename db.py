import sqlite3
from config import DB_PATH


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    """确保所有必要的表存在，启动时调用"""
    conn = get_db_connection()

    # 旧库必须先离线迁移，避免启动后形成新旧字段混用的数据库。
    for table in ('users', 'sign_ins', 'weekly_plans', 'weekly_plan_items',
                  'daily_completions', 'monthly_summaries', 'okr_key_results', 'llm_calls'):
        cols = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        if cols and ('uid' in cols or 'loginname' not in cols
                     or (table == 'sign_ins' and 'card_uid' not in cols)):
            conn.close()
            raise RuntimeError(
                f'数据库 {table} 仍为旧结构，请先运行 scripts/migrate_account_identity.py '
                '迁移到新库，再修改 .config.yml 的 db_path。')

    # 业务记录保留自身 id 主键，人员统一用 loginname 关联；未绑定签到允许账号为空。
    # card_uid 仅记录刷卡来源，后续解绑/换卡不应改变已经确认的人员归属。
    conn.execute('''CREATE TABLE IF NOT EXISTS sign_ins
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     loginname TEXT REFERENCES users(loginname),
                     card_uid TEXT,
                     timestamp DATETIME)''')
    # 用户身份以 loginname 为主键；卡片关系单独保存，支持一个账号绑定多张卡。
    # 此处仅定义新库结构，CREATE TABLE IF NOT EXISTS 不会修改已有表的主键。
    conn.execute('''CREATE TABLE IF NOT EXISTS users
                    (loginname TEXT PRIMARY KEY NOT NULL, name TEXT)''')

    # id 用于页面管理绑定，uid 仍唯一；loginname 不设 UNIQUE，允许一人多卡。
    conn.execute('''CREATE TABLE IF NOT EXISTS user_cards
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     uid TEXT NOT NULL UNIQUE,
                     loginname TEXT NOT NULL REFERENCES users(loginname))''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_cards_loginname "
                 "ON user_cards(loginname)")

    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_plans
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     loginname TEXT NOT NULL REFERENCES users(loginname),
                     week_key TEXT NOT NULL,
                     content TEXT NOT NULL,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(loginname, week_key))''')
    # weekly plan 的 TODO 列表（itemized）；status 供后续 daily completion 标记完成（done）
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_plan_items
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     loginname TEXT NOT NULL REFERENCES users(loginname),
                     week_key TEXT NOT NULL,
                     item_order INTEGER NOT NULL,
                     text TEXT NOT NULL,
                     status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
                     completed_date TEXT,
                     completed_at DATETIME,
                     created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wpi_loginname_week "
                 "ON weekly_plan_items(loginname, week_key)")
    
    # review：上一工作周期总结（必填）；todo：下一工作周期计划（可选）
    # 旧库的 content 列已通过迁移脚本改为 review（旧值即 review），todo 回填为 NULL
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_completions
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     loginname TEXT NOT NULL REFERENCES users(loginname),
                     date TEXT NOT NULL,
                     review TEXT NOT NULL,
                     todo TEXT,
                     submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(loginname, date))''')
    
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
                    (loginname TEXT NOT NULL REFERENCES users(loginname),
                     month_key TEXT NOT NULL,
                     summary TEXT,
                     suggestion TEXT,
                     generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                     UNIQUE(loginname, month_key))''')
   
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
                     loginname TEXT NOT NULL REFERENCES users(loginname),
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
                     loginname TEXT REFERENCES users(loginname),
                     model TEXT,
                     prompt TEXT,
                     response TEXT,
                     status TEXT NOT NULL DEFAULT 'success' CHECK(status IN ('success','error')),
                     token_usage TEXT,
                     error TEXT,
                     requested_at DATETIME)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")

    # ===== ZGantt 项目管理 =====
    # zgantts：start_date 建项当天且不可变(永不 UPDATE)；status 在研(active)->结项(closed) 单向
    conn.execute('''CREATE TABLE IF NOT EXISTS zgantts
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     name TEXT NOT NULL,
                     status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
                     start_date TEXT NOT NULL,
                     created_by TEXT NOT NULL,
                     closed_at DATETIME,
                     created_at DATETIME DEFAULT (datetime('now','localtime')))''')
    # 项目成员：永不删除，仅 status->departed；重新加人=复活(转回active,清departed_at)
    conn.execute('''CREATE TABLE IF NOT EXISTS zgantt_members
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     zgantt_id INTEGER NOT NULL REFERENCES zgantts(id),
                     loginname TEXT NOT NULL,
                     status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','departed')),
                     departed_at DATETIME,
                     created_at DATETIME DEFAULT (datetime('now','localtime')),
                     UNIQUE(zgantt_id, loginname))''')
    # 工作组：相关工作项的集合(无进度概念)；两级排序第一键=created_at
    conn.execute('''CREATE TABLE IF NOT EXISTS work_groups
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     zgantt_id INTEGER NOT NULL REFERENCES zgantts(id),
                     name TEXT NOT NULL,
                     created_by TEXT NOT NULL,
                     created_at DATETIME DEFAULT (datetime('now','localtime')))''')
    # 工作项：owner=创建者固定；end_date NULL=进行中, 非空=已完成(此时 summary 必填)
    # zgantt_id 冗余(可经 group 导出)，便于按项目单表扫描；两级排序第二键=created_at
    conn.execute('''CREATE TABLE IF NOT EXISTS work_items
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     group_id INTEGER NOT NULL REFERENCES work_groups(id),
                     zgantt_id INTEGER NOT NULL REFERENCES zgantts(id),
                     title TEXT NOT NULL,
                     owner TEXT NOT NULL,
                     start_date TEXT NOT NULL,
                     end_date TEXT,
                     summary TEXT,
                     created_at DATETIME DEFAULT (datetime('now','localtime')))''')
    # 考勤：每个(zgantt,成员,日期)一行；改填=INSERT OR REPLACE，清空=DELETE
    # att_type: half=半天(0.5) / full=全天(1.0) / overtime=加班(1.5)
    conn.execute('''CREATE TABLE IF NOT EXISTS zgantt_attendance
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     zgantt_id INTEGER NOT NULL REFERENCES zgantts(id),
                     loginname TEXT NOT NULL,
                     date TEXT NOT NULL,
                     att_type TEXT NOT NULL CHECK(att_type IN ('half','full','overtime')),
                     created_at DATETIME DEFAULT (datetime('now','localtime')),
                     UNIQUE(zgantt_id, loginname, date))''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zg_att_user_month "
                 "ON zgantt_attendance(zgantt_id, loginname, date)")

    # 上面新增的表（zgantts 系列、llm_calls 等）直接用 DEFAULT (datetime('now','localtime'))，
    # 建表即为本地时间，无需触发器。下面这些「老表」在生产库里已存在，CREATE TABLE IF NOT EXISTS
    # 不会改它们的 DEFAULT（仍是 CURRENT_TIMESTAMP=UTC），故保留 AFTER INSERT 触发器，把新写入
    # 改成 datetime('now','localtime')（服务器本地时区）。DROP+CREATE 保证触发器体始终与代码一致。
    for _tbl, _col in [
        ('weekly_plans', 'submitted_at'), ('weekly_plan_items', 'created_at'),
        ('daily_completions', 'submitted_at'), ('booking_slots', 'created_at'),
        ('bookings', 'booked_at'), ('monthly_summaries', 'generated_at'),
        ('okr_cycles', 'created_at'), ('okr_objectives', 'created_at'),
        ('okr_key_results', 'created_at'), ('okr_kr_milestones', 'created_at'),
    ]:
        conn.execute("DROP TRIGGER IF EXISTS tg_{0}_{1}".format(_tbl, _col))
        conn.execute(
            "CREATE TRIGGER tg_{0}_{1} AFTER INSERT ON {0} "
            "BEGIN UPDATE {0} SET {1}=datetime('now','localtime') WHERE rowid=new.rowid; END".format(_tbl, _col))

    conn.commit()
    conn.close()
