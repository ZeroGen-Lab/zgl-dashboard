# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ZGL 组织活跃看板系统，包含 IC 卡签到、团队工作规划/进度跟踪、预约广场功能。采用端侧-服务端分离架构。Web 页面需要登录认证（用户名来自 `.users.txt`，密码规则见源码），API 接口需要 HMAC-SHA256 按日轮换 token 认证。签到 API 额外要求端侧 IP 白名单。

## Running

```bash
# 服务端（Flask dashboard）
python app.py                    # 启动在 0.0.0.0:5000，数据库表自动创建

# 端侧（树莓派刷卡客户端，需 root 权限访问 USB 设备）
sudo python3 checkin_usb.py
```

建议部署为 systemd 服务。

## Architecture

**端侧-服务端分离 + 服务端 Blueprint 模块化架构：**

- **`app.py`**（入口）：创建 Flask app，加载配置，注册 4 个 Blueprint，启动服务
- **`config.py`**：从 `.config.yml` 加载配置，导出模块级全局变量（`DB_PATH`, `API_SECRET`, `ALLOWED_CHECKIN_IPS`, `SECRET_KEY`）
- **`db.py`**：数据库连接（`get_db_connection()`）和 6 张表的初始化（`ensure_tables()`）
- **`auth.py`**：认证基础设施——用户集（`.users.txt`）、HMAC token 生成/验证、装饰器（`login_required`, `token_required`, `checkin_ip_required`）
- **`helpers.py`**：业务辅助函数（`compute_week_key`, `compute_upcoming_instances`）
- **`routes_dashboard.py`**：Blueprint——首页、绑定、统计、详情
- **`routes_booking.py`**：Blueprint（url_prefix='/booking'）——预约广场全部路由
- **`routes_api.py`**：Blueprint（url_prefix='/api'）——签到、每周计划、每日完成 API
- **`routes_auth.py`**：Blueprint——登录/登出
- **`checkin_usb.py`**（端侧/树莓派）：通过 `evdev` 读取 USB IC 读卡器输入，刷卡后先写本地 SQLite，再 HTTP POST 同步到服务端（请求头带 `Authorization: Bearer <HMAC-token>`）。本地表有 `synced` 字段追踪同步状态（0=未同步, 1=已同步）。`retry_sync` 每日自动重试7天内未同步记录（`threading.Timer(86400)`）。

**数据同步流：** 读卡器 → 本地 DB 写入 → HTTP POST `/api/checkin`（带 HMAC token） → 服务端 DB 写入

**端侧与服务端的 `sign_ins` 表结构不同：**
- 服务端：`id, uid, timestamp`（无 synced 字段）
- 端侧：`id, uid, timestamp, synced`

**认证机制：**
- Web 页面：Session 登录认证（`@login_required`），用户名密码校验
- API 接口：HMAC-SHA256 按日轮换 token（`@token_required`），服务端和客户端共享 `API_SECRET`
- 签到 API：IP 白名单（`@checkin_ip_required`），只允许树莓派等端侧设备

## Database

SQLite（`attendance.db`），六张表：
- `sign_ins`：签到记录（id, uid, timestamp），端侧额外有 synced 字段
- `users`：UID-姓名绑定（uid PK, name）
- `weekly_plans`：每周计划（id, uid, week_key, content, submitted_at），UNIQUE(uid, week_key)
- `daily_completions`：每日完成情况（id, uid, date, content, submitted_at），UNIQUE(uid, date)
- `booking_slots`：预约时段（id, publisher, slot_type, title, description, day_of_week, start_hour, end_hour, specific_date, capacity, status, created_at），slot_type 为 'recurring' 或 'one_time'
- `bookings`：预约记录（id, slot_id, booker, instance_date, status, booked_at），UNIQUE(slot_id, booker, instance_date)

## Key API Endpoints

| Route | Method | Auth | Description |
|-------|--------|------|-------------|
| `/` | GET | login | 看板首页，最近刷卡记录 + 绑定（铅笔编辑）+ 一次性预约轮播 |
| `/login` | GET/POST | none | 登录页 |
| `/logout` | GET | none | 登出 |
| `/bind` | POST | login | 表单绑定 UID 与姓名 |
| `/stats` | GET | login | 考勤统计（15/60/180天+上月）+ 柱状图 + 热力图 |
| `/detail/<uid>` | GET | login | 单人日历详情（4周视图，支持 ?offset=N 翻页） |
| `/booking` | GET | login | 预约广场主页 |
| `/booking/publish` | GET/POST | login | 发布新预约时段 |
| `/booking/book/<slot_id>/<instance_date>` | POST | login | 预约某个时段实例 |
| `/booking/cancel/<booking_id>` | POST | login | 取消我的预约 |
| `/booking/cancel_slot/<slot_id>` | POST | login | 取消我发布的时段 |
| `/api/checkin` | POST | token + IP白名单 | JSON `{"uid", "timestamp"}` 签到同步接口 |
| `/api/weekly_plan` | POST | token | JSON `{"uid", "content"}` 每周计划（周六至周一中午12点） |
| `/api/daily_completion` | POST | token | JSON `{"uid", "date", "content"}` 每日完成情况 |

## Templates

- `templates/base.html` — 公共布局（sticky navbar、水印矩阵、flash 消息、Bootstrap JS/Icons/Chart.js CDN）
- `templates/login.html` — 登录页
- `templates/index.html` — 首页（最近刷卡 + 铅笔编辑绑定 + 一次性预约轮播）
- `templates/stats.html` — 统计页（表格 + 柱状图 + 56天热力图）
- `templates/detail.html` — 日历详情页（4周视图 + 计划/完成 + 近30条刷卡明细）
- `templates/booking.html` — 预约广场主页（一次性/长期卡片 + 我的预约/发布）
- `templates/booking_publish.html` — 发布预约时段表单

## Configuration

所有敏感配置集中在 `.config.yml`，由 `config.py`（服务端）和 `checkin_usb.py` 通过 `yaml.safe_load()` 统一读取：
- 服务端：`db_path`、`secret_key`、`api_secret`、`allowed_checkin_ips`
- 端侧：`server_url`、`db_path`、`api_secret`

部署流程：`cp config.example.yml .config.yml` → 编辑实际值

`.config.yml` 和 `.users.txt` 已在 `.gitignore` 中，不会提交到仓库。

## File List

- `app.py` — 服务端入口（Flask app + Blueprint 注册）
- `config.py` — 配置加载（.config.yml → 模块级全局）
- `db.py` — 数据库连接与表初始化
- `auth.py` — 认证基础设施（用户集、HMAC token、装饰器）
- `helpers.py` — 业务辅助函数
- `routes_auth.py` — Blueprint: 登录/登出
- `routes_dashboard.py` — Blueprint: 首页/绑定/统计/详情
- `routes_booking.py` — Blueprint: 预约广场
- `routes_api.py` — Blueprint: 签到/计划/完成 API
- `checkin_usb.py` — 树莓派刷卡客户端
- `templates/` — Jinja2 模板
- `environment.yml` — Conda 环境定义（`flask_app`，Python 3.10）
- `.config.yml` — 运行配置（敏感，不提交）
- `config.example.yml` — 配置示例模板
- `.users.txt` — 合法用户名列表（敏感，不提交）
- `.gitignore` — 忽略规则

## Dependencies

第三方依赖（见 `environment.yml`）：Flask、flask-cors、requests、PyYAML、evdev、gunicorn
标准库依赖（无需安装）：sqlite3、hmac、hashlib、os、datetime、threading