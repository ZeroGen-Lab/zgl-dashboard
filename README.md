# ZGL Dashboard

ZGL 团队组织活跃看板系统，包含 IC 卡签到、团队工作规划与进度跟踪、预约广场功能。采用端侧-服务端分离架构。

## 架构

```
读卡器(USB) → 树莓派(checkin_usb.py) → HTTP POST → 服务端(Flask) → Web看板
```

- **服务端**：Flask Web 应用（Blueprint 模块化），提供看板页面、预约广场和签到/计划 API
- **端侧** `checkin_usb.py`：树莓派上运行，通过 evdev 读取 USB IC 读卡器，刷卡后先写本地 SQLite，再同步到服务端

## 快速开始

### 1. 创建环境

```bash
conda env create -f environment.yml
conda activate dashboard_app
```

### 2. 配置

```bash
cp config.example.yml .config.yml
# 编辑 .config.yml，填写实际的 secret_key、api_secret、IP 等
```

### 3. 创建用户列表

```bash
# 创建 .users.txt，每行一个用户名
echo "your_username" > .users.txt
```

### 4. 启动服务端

```bash
python app.py       # 启动在 0.0.0.0:5000
python app.py pre   # 预发环境，启动在 0.0.0.0:5001 (见config.yml)
```

### 5. 启动端侧（可部署在树莓派上）

```bash
sudo python3 checkin_usb.py
```

建议将以上服务端与端侧程序都部署为 systemd 服务，如服务端服务配置文件存于
/etc/systemd/system/dashboard.service

```bash
[Unit]
Description=ZGL Dashboard Flask App
After=network.target

[Service]
Type=simple
User=xxx
WorkingDirectory=/xxx/.../zgl-dashboard
ExecStart=/.../bin/python app.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Linux 系统服务 (Systemd) 相关命令：
```bash
sudo systemctl start dashboard      # 启动服务
sudo systemctl stop dashboard       # 停止服务
sudo systemctl restart dashboard    # 重启服务
sudo systemctl enable dashboard     # 开机自启
sudo systemctl disable dashboard    # 取消开机自启
sudo systemctl status dashboard     # 查看服务状态

sudo systemctl daemon-reload        # 修改dashboard.service 配置后必须让系统重新加载配置，否则修改不生效。

sudo journalctl -u dashboard -f     # 实时滚动查看新日志
sudo journalctl -u dashboard --since "1 hour ago"   # 查看最近 1 小时内的日志
```

## 登录认证

- Web 页面：用户名来自 `.users.txt`，密码规则见源码
- API 接口：HMAC-SHA256 按日轮换 token（`Authorization: Bearer <token>`）
- 签到 API 额外要求 IP 白名单（只允许树莓派等端侧设备）

## 数据库

SQLite（`attendance.db`），首次启动自动创建：

| 表 | 说明 |
|---|------|
| sign_ins | 签到记录（id, loginname, card_uid, timestamp）；未知卡的 loginname 为空 |
| users | 成员账号与姓名（loginname 主键, name） |
| user_cards | 卡片绑定（id, uid 唯一, loginname）；支持一人多卡 |
| weekly_plans | 每周计划（loginname, week_key, content），每账号每周唯一 |
| daily_completions | 日报（loginname, date, review, todo），每账号每天唯一 |
| monthly_summaries | AI 月度摘要与建议（loginname, month_key） |
| okr_key_results | 关键结果，负责人使用 loginname |
| booking_slots | 预约时段（publisher, slot_type, title, start/end_hour, capacity）。start/end_hour 为浮点小时（`6.5`=6:30），范围 6:00–22:00、整点与半点可选 |
| bookings | 预约记录（slot_id, booker, instance_date） |

旧库启动时会明确提示迁移；参考 [迁移说明](scripts/README_migration.md)。数据库连接已开启外键校验，迁移脚本不会自动修改应用配置。

## Web 页面

| 页面 | 说明 |
|------|------|
| 首页 | 成员与卡片管理；点击绑定至当前账号；清理超过三天且无归属、未绑定的刷卡记录 |
| 统计 | 考勤统计 + 柱状图 + 56天热力图 |
| 详情 | 按账号展示上周、本周和下周计划及日报 |
| 预约广场 | 发布/浏览/预约/编辑空闲时段（长期 + 一次性；活动过了结束时间仍可见但只读，昨日及更早的从广场隐藏） |

## API

| 接口 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/api/checkin` | POST | token + IP白名单 | 签到同步 |
| `/api/weekly_plan` | POST | token | 每周计划（周六至周一提交窗口，与网页共用规则） |
| `/api/daily_completion` | POST | token | 每日完成情况 |
| `/booking/` | GET | login | 预约广场主页 |
| `/booking/publish` | GET/POST | login | 发布新预约时段（时间 6:00–22:00，整点+半点） |
| `/booking/edit/<id>` | GET/POST | login | 编辑已发布时段（除类型外字段可改） |
| `/booking/book/<id>/<date>` | POST | login | 预约某个时段实例（已结束的实例会被拒绝） |
| `/booking/cancel/<id>` | POST | login | 取消预约 |
| `/booking/cancel_slot/<id>` | POST | login | 取消已发布的时段 |

### API 身份字段

- `/api/checkin` 继续接收设备的 `uid`（字符串）和可选 `timestamp`（`YYYY-MM-DD HH:MM:SS`）。服务端在同一事务中查询当前卡片归属，写入 `loginname` 和 `card_uid`，不采用客户端声称的账号。
- `/api/weekly_plan`、`/api/daily_completion` 支持 `loginname`，也兼容旧客户端的卡号 `uid`。传卡号时必须已绑定；两者同时传入必须匹配。
- 账号必须存在于 `users`；API Token 仍是可信客户端凭据，未改为面向普通用户的账号授权机制。
- 周计划接收字符串列表 `items`，也兼容以换行分隔的 `content`；日报接收 `review` 和可选 `todo`，兼容用 `content` 表示回顾。
- 响应提供 `loginname`，旧请求提交 `uid` 时响应继续保留 `uid`。
- 统计、周报、月报按账号汇总，同日多卡签到只计一个出勤日，未归属签到不计入成员统计。月度 AI 结果保存到页面选中的月份。
- 离线补传仍按服务端接收时的当前绑定解析账号。当前库没有绑定时间线，跨改绑补传的历史持卡人不能自动恢复；本次未增加绑定历史表。

新账号请求示例（仍需原有 Token）：

```json
{"loginname": "alice", "items": ["完成接口适配"]}
```

```json
{"loginname": "alice", "review": "完成数据库迁移验证", "todo": "进行联调"}
```

## 文件说明

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
- `templates/` — Jinja2 模板（Bootstrap 5 + Chart.js + Carousel）
- `environment.yml` — Conda 环境定义
- `.config.yml` — 运行配置（敏感，不提交）
- `config.example.yml` — 配置示例模板
- `.users.txt` — 合法用户名列表（敏感，不提交）
