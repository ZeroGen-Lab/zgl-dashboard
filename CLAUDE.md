# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ZGL 组织活跃看板系统，包含 IC 卡签到、团队工作规划/进度跟踪、预约广场、周报摘要功能。采用端侧-服务端分离架构。Web 页面需要登录认证（用户名来自 `.users.txt`，密码规则见源码），API 接口需要 HMAC-SHA256 按日轮换 token 认证。签到 API 额外要求端侧 IP 白名单。

## Running

```bash
# 服务端（Flask dashboard）
python app.py                    # 启动在 0.0.0.0:5000，数据库表自动创建

# 预发环境（本地测试）
python app.py pre                # 启动在 0.0.0.0:5101，使用 attendance_pre.db

# 端侧（树莓派刷卡客户端，需 root 权限访问 USB 设备）
sudo python3 checkin_usb.py
```

**本地测试请使用 `python app.py pre`（端口 5101）**，避免影响生产数据。

建议部署为 systemd 服务。

## Architecture

**端侧-服务端分离 + 服务端 Blueprint 模块化架构：**

- **`app.py`**（入口）：创建 Flask app，加载配置，注册 7 个 Blueprint，注册 `fmt_time` Jinja 过滤器（浮点小时 → `"HH:MM"`，如 `6.5` → `06:30`，用于 booking 半点时间显示），启动服务；若配置了钉钉 webhook 则启动 APScheduler 周一 18:30 定时推送 onsite 报告（每人上周 onsite + 本月 onsite 达标进度，达标线=本月工作日-2，进度条展示）
- **`config.py`**：从 `.config.yml` 加载配置，导出模块级全局变量（`DB_PATH`, `API_SECRET`, `ALLOWED_CHECKIN_IPS`, `SECRET_KEY`, `DINGTALK_WEBHOOK_URL`, `DINGTALK_SECRET`）
- **`db.py`**：数据库连接（`get_db_connection()`）和 21 张表的初始化（`ensure_tables()`）
- **`auth.py`**：认证基础设施——用户集（`.users.txt`）、HMAC token 生成/验证、装饰器（`login_required`, `token_required`, `checkin_ip_required`）
- **`helpers.py`**：业务辅助函数（`compute_week_key`, `compute_upcoming_instances`, `is_instance_expired`, `compute_summary_week_range`, `generate_weekly_summary`）。`is_instance_expired(slot, date)` 判断 booking 实例是否已过结束时间（`now ≥ 当日 + end_hour`），用于实效只读判定
- **`notifier.py`**：钉钉群机器人消息推送（`send_dingtalk_markdown`），支持 HMAC-SHA256 加签
- **`routes_dashboard.py`**：Blueprint——首页、绑定、统计、详情、周报摘要
- **`routes_booking.py`**：Blueprint（url_prefix='/booking'）——预约广场全部路由
- **`routes_api.py`**：Blueprint（url_prefix='/api'）——签到、每周计划、每日完成 API
- **`routes_auth.py`**：Blueprint——登录/登出
- **`routes_okr.py`**：Blueprint（url_prefix='/okr'）——OKR 管理
- **`routes_zgantt.py`**：Blueprint（url_prefix='/zgantt'）——ZGantt 项目管理（项目甘特图 + 项目考勤）
- **`routes_agrant.py`**：Blueprint（url_prefix='/agrant'）——学术交流经费（AGrant）管理：管理员页头表单授予/追回额度（两个金额输入互斥，只能填一个）、学生申报使用、同学间转赠（手续费 10%）、毕业冻结
- **`checkin_usb.py`**（端侧/树莓派）：通过 `evdev` 读取 USB IC 读卡器输入，刷卡后先写本地 SQLite，再 HTTP POST 同步到服务端（请求头带 `Authorization: Bearer <HMAC-token>`）。本地表有 `synced` 字段追踪同步状态（0=未同步, 1=已同步）。`retry_sync` 每日自动重试7天内未同步记录（`threading.Timer(86400)`）。

**数据同步流：** 读卡器 → 本地 DB 写入 → HTTP POST `/api/checkin`（带 HMAC token） → 服务端 DB 写入

**端侧与服务端的 `sign_ins` 表结构不同：**
- 服务端：`id, loginname, card_uid, timestamp`（无 synced 字段）
- 端侧：`id, uid, timestamp, synced`

**认证机制：**
- Web 页面：Session 登录认证（`@login_required`），用户名密码校验
- API 接口：HMAC-SHA256 按日轮换 token（`@token_required`），服务端和客户端共享 `API_SECRET`
- 签到 API：IP 白名单（`@checkin_ip_required`），只允许树莓派等端侧设备

## Database

SQLite（`attendance.db` / `attendance_pre.db`），21 张表由 `db.py: ensure_tables()` 创建。所有"行创建时刻"型时间戳列的默认值统一为 `datetime('now','localtime')`（服务器本地时间）；业务时刻/生命周期时刻列（如 sign_ins.timestamp、closed_at、departed_at）无默认值。数据库连接开启外键校验（`PRAGMA foreign_keys=ON`）。

**身份与卡片：**
- `users`：账号（loginname PK, name, email）。email 全局唯一（UNIQUE，允许多行 NULL）；**登录即建档**（name 以登录名占位），姓名/邮箱仅本人经 `/update_profile` 修改
- `user_cards`：卡片绑定（id, uid UNIQUE, loginname FK users）；一人可多卡，一卡只属于一人
- `sign_ins`：签到记录（id, loginname FK 可空, card_uid, timestamp）。归属在落库时按当时绑定解析；未绑定卡 loginname 为 NULL。绑定认领该卡全部 NULL 记录；解绑时由用户选择保留原账号归属，或将该卡历史签到归属置空（签到行永不删除，置空后重新绑定可再认领）

**工作计划/日报/月报：**
- `weekly_plans`：每周计划（id, loginname, week_key, content, submitted_at），UNIQUE(loginname, week_key)
- `weekly_plan_items`：计划 TODO 条目（id, loginname, week_key, item_order, text, status∈{open,done}, completed_date, completed_at, created_at）
- `daily_completions`：日报（id, loginname, date, review 必填, todo 可选, submitted_at），UNIQUE(loginname, date)
- `monthly_summaries`：AI 月报缓存（loginname, month_key, summary, suggestion, generated_at），UNIQUE(loginname, month_key)

**预约广场：**
- `booking_slots`：预约时段（id, publisher, slot_type∈{recurring,one_time}, title, description, day_of_week, start_hour, end_hour, specific_date, capacity, status∈{active,cancelled}, created_at）。`start_hour`/`end_hour` 为 **REAL（浮点小时，`6.5`=6:30）**，范围 6.0–22.0、整点与半点可选
- `bookings`：预约记录（id, slot_id, booker, instance_date, status, booked_at），UNIQUE(slot_id, booker, instance_date)

**OKR：**
- `okr_cycles`：周期（id, cycle_key UNIQUE, status∈{brainstorming,planning,active,closed}, created_by, created_at）
- `okr_objectives`：目标 O（id, cycle_id FK, title, description, status∈{draft,approved,rejected}, proposed_by, created_at）
- `okr_key_results`：关键结果 KR（id, objective_id FK, loginname FK, title, description, progress 0–100, status∈{active,pending_edit}, pending_title, pending_description, created_at）
- `okr_kr_milestones`：里程碑（id, kr_id FK, description, created_at）

**LLM 追踪与项目管理（ZGantt）：**
- `llm_calls`：LLM 调用追踪（id, request_id UNIQUE, session_id, loginname, model, prompt, response, status∈{success,error}, token_usage, error, requested_at）；prompt/token_usage 存 JSON 字符串
- `zgantts`：项目（id, name, status∈{active,closed}, start_date 建项日不可变, created_by, closed_at, created_at）
- `zgantt_members`：项目成员（id, zgantt_id, loginname, status∈{active,departed}, departed_at, created_at），UNIQUE(zgantt_id, loginname)；永不删除，离组=departed，重新加入=复活
- `work_groups`：工作组（id, zgantt_id, name, created_by, created_at）
- `work_items`：工作项（id, group_id, zgantt_id 冗余便于单表扫描, title, owner, start_date, end_date, summary, created_at）；end_date NULL=进行中，非空=已完成（此时 summary 必填）
- `zgantt_attendance`：项目考勤（id, zgantt_id, loginname, date, att_type∈{half,full,overtime}, created_at），UNIQUE(zgantt_id, loginname, date)

**学术交流经费（AGrant）：**
- `academic_grant_accounts`：经费账户（loginname PK, balance INTEGER 元 CHECK≥-500（允许透支至 -500 元）, status∈{active,graduated}, graduated_at, created_at）。首次授予即开户（仅限已完成姓名绑定的用户）；毕业仅作用于本页面：冻结一切额度变动（授予/调整/申报/转出/转入），记录保留可见，可撤销（graduated_at 置空复活，同 zgantt_members.departed 模式）
- `academic_grant_records`：经费流水（id, loginname, record_type∈{grant,adjust,usage,transfer_out,transfer_in}, amount 有符号增量元 CHECK≠0, note, related_loginname 转赠对方, created_by, created_at）；只增不改不删，对账恒等式：每账户 SUM(amount)==balance。grant=正（授予，页头表单「授予金额」仅正向）/adjust=负（管理员追回扣减，页头表单「追回金额」输入正数落负账）/usage=负（本人申报）/transfer_out=负、transfer_in=正（转赠实到额）；转赠手续费 10%（received=(amount*9+5)//10，实收 90% 四舍五入到整数），手续费不入账即自然损耗；转出额与追回扣减额均不得超过当前余额（两者不透支，透支下限 -500 仅适用于申报使用）

**存量库迁移：** 老库（UTC 默认值 + 补丁触发器结构）用 `scripts/rebuild_schema.py` 一次性重建（改名临时表→按最新 DDL 重建→拷回→删临时表；自动备份，外键/行数/完整性校验通过才提交）。

## Key API Endpoints

| Route | Method | Auth | Description |
|-------|--------|------|-------------|
| `/` | GET | login | 看板首页，最近刷卡记录 + 绑定（铅笔编辑）+ 一次性预约轮播 |
| `/login` | GET/POST | none | 登录页 |
| `/logout` | GET | none | 登出 |
| `/bind` | POST | login | 表单绑定 UID 与姓名 |
| `/stats` | GET | login | 考勤统计（15/60/180天+上月）+ 柱状图 + 热力图 |
| `/detail/<uid>` | GET | login | 单人日历详情（4周视图，支持 ?offset=N 翻页） |
| `/weekly_summary` | GET | login | 周报摘要（出勤天数 + 日报数 + 周计划提交状态，支持 ?offset=N 翻页） |
| `/booking/` | GET | login | 预约广场主页（失效活动仍可见但只读，昨日及更早的从广场卡片隐藏） |
| `/booking/publish` | GET/POST | login | 发布新预约时段（时间 6:00–22:00，整点+半点） |
| `/booking/edit/<slot_id>` | GET/POST | login | 编辑已发布时段（除类型外：标题/说明/起止时间/容量/日期或星期几；已失效的一次性活动不可编辑） |
| `/booking/book/<slot_id>/<instance_date>` | POST | login | 预约某个时段实例（已结束的实例会被拒绝） |
| `/booking/cancel/<booking_id>` | POST | login | 取消我的预约 |
| `/booking/cancel_slot/<slot_id>` | POST | login | 取消我发布的时段 |
| `/agrant/` | GET | login | AGrant 经费主页（每人卡片：余额徽章高亮 + 流水倒序；?scope=active 默认/all 含毕业） |
| `/agrant/grant` | POST | login + admin | 授予额度（页头管理员表单「授予金额」，仅正数，唯一追加入口）；首次授予即开户，仅限已完成姓名绑定的用户 |
| `/agrant/adjust` | POST | login + admin | 追回扣减额度（页头管理员表单「追回金额」+ loginname；仅负向：输入正数追回额，落账为负 adjust 流水，不得超过当前余额）。与 /grant 共用同一表单，两个金额互斥（前端+后端双重校验） |
| `/agrant/usage` | POST | login | 本人申报使用（amount 正整数 + note 必填），直接扣减，无需审批 |
| `/agrant/transfer` | POST | login | 转赠额度（to + amount；手续费 10%，received=(amount*9+5)//10 四舍五入，转出额不得超过当前余额） |
| `/agrant/graduate/<loginname>` | POST | login + admin | 标记毕业（版块冻结只读，记录保留） |
| `/agrant/revoke/<loginname>` | POST | login + admin | 撤销毕业（恢复操作） |
| `/api/checkin` | POST | token + IP白名单 | JSON `{"uid", "timestamp"}` 签到同步接口 |
| `/api/weekly_plan` | POST | token | JSON `{"uid", "content"}` 每周计划（周六至周一中午12点） |
| `/api/daily_completion` | POST | token | JSON `{"uid", "date", "content"}` 每日完成情况 |

## Templates

- `templates/base.html` — 公共布局（sticky navbar、水印矩阵、flash 消息、Bootstrap JS/Icons/Chart.js CDN）
- `templates/login.html` — 登录页
- `templates/index.html` — 首页（最近刷卡 + 铅笔编辑绑定 + 一次性预约轮播）
- `templates/stats.html` — 统计页（表格 + 柱状图 + 56天热力图）
- `templates/detail.html` — 日历详情页（4周视图 + 计划/完成 + 近30条刷卡明细）
- `templates/booking.html` — 预约广场主页（一次性/长期卡片 + 我的预约/发布；失效实例显示「已结束」只读、发布者可编辑）
- `templates/booking_publish.html` — 发布/编辑预约时段表单（双用，经 `fmt_time` 渲染 6:00–22:00 半点时间下拉）
- `templates/weekly_summary.html` — 周报摘要页（出勤/日报/周计划表格 + 翻页）
- `templates/zgantt.html` — ZGantt 项目页（项目/成员管理 + 甘特图 + 考勤填报）
- `templates/agrant.html` — AGrant 经费页（余额徽章高亮 + 每人流水倒序卡片 + 管理员页头「授予/追回」合一表单（互斥双金额输入，JS 按所填项切换提交目标）+ 申报/转赠内联展开表单 + 毕业 confirm modal，仿 okr.html）

## Configuration

所有敏感配置集中在 `.config.yml`，由 `config.py`（服务端）和 `checkin_usb.py` 通过 `yaml.safe_load()` 统一读取：
- 服务端：`db_path`、`secret_key`、`api_secret`、`allowed_checkin_ips`、`dingtalk_webhook_url`、`dingtalk_secret`
- 端侧：`server_url`、`db_path`、`api_secret`

部署流程：`cp config.example.yml .config.yml` → 编辑实际值

`.config.yml` 和 `.users.txt` 已在 `.gitignore` 中，不会提交到仓库。

## File List

- `app.py` — 服务端入口（Flask app + Blueprint 注册 + APScheduler 定时任务）
- `config.py` — 配置加载（.config.yml → 模块级全局）
- `db.py` — 数据库连接与表初始化
- `auth.py` — 认证基础设施（用户集、HMAC token、装饰器）
- `helpers.py` — 业务辅助函数
- `notifier.py` — 钉钉群机器人消息推送
- `routes_auth.py` — Blueprint: 登录/登出
- `routes_dashboard.py` — Blueprint: 首页/绑定/统计/详情
- `routes_booking.py` — Blueprint: 预约广场
- `routes_api.py` — Blueprint: 签到/计划/完成 API
- `routes_okr.py` — Blueprint: OKR 管理
- `routes_zgantt.py` — Blueprint: ZGantt 项目管理
- `routes_agrant.py` — Blueprint: 学术交流经费（AGrant）管理
- `checkin_usb.py` — 树莓派刷卡客户端
- `templates/` — Jinja2 模板
- `environment.yml` — Conda 环境定义（`flask_app`，Python 3.10）
- `.config.yml` — 运行配置（敏感，不提交）
- `config.example.yml` — 配置示例模板
- `.users.txt` — 合法用户名列表（敏感，不提交）
- `.gitignore` — 忽略规则

## Dependencies

第三方依赖（见 `environment.yml`）：Flask、flask-cors、requests、PyYAML、apscheduler、evdev、gunicorn
标准库依赖（无需安装）：sqlite3、hmac、hashlib、os、datetime、threading