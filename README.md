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
python app.py    # 启动在 0.0.0.0:5000
```

### 5. 启动端侧（可部署在树莓派上）

```bash
sudo python3 checkin_usb.py
```

建议将以上服务端与端侧程序都部署为 systemd 服务。

## 登录认证

- Web 页面：用户名来自 `.users.txt`，密码规则见源码
- API 接口：HMAC-SHA256 按日轮换 token（`Authorization: Bearer <token>`）
- 签到 API 额外要求 IP 白名单（只允许树莓派等端侧设备）

## 数据库

SQLite（`attendance.db`），首次启动自动创建：

| 表 | 说明 |
|---|------|
| sign_ins | 签到记录（id, uid, timestamp） |
| users | UID-姓名绑定（uid, name） |
| weekly_plans | 每周计划（uid, week_key, content） |
| daily_completions | 每日完成情况（uid, date, content） |
| booking_slots | 预约时段（publisher, slot_type, title, start/end_hour, capacity） |
| bookings | 预约记录（slot_id, booker, instance_date） |

## Web 页面

| 页面 | 说明 |
|------|------|
| 首页 | 最近刷卡记录 + UID/姓名绑定 + 一次性预约轮播 |
| 统计 | 考勤统计 + 柱状图 + 56天热力图 |
| 详情 | 单人日历视图（4周） + 计划/完成 |
| 预约广场 | 发布/浏览/预约空闲时段（长期 + 一次性） |

## API

| 接口 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/api/checkin` | POST | token + IP白名单 | 签到同步 |
| `/api/weekly_plan` | POST | token | 每周计划（周六至周一中午12点） |
| `/api/daily_completion` | POST | token | 每日完成情况 |
| `/booking` | GET | login | 预约广场主页 |
| `/booking/publish` | GET/POST | login | 发布新预约时段 |
| `/booking/book/<id>/<date>` | POST | login | 预约某个时段实例 |
| `/booking/cancel/<id>` | POST | login | 取消预约 |
| `/booking/cancel_slot/<id>` | POST | login | 取消已发布的时段 |

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