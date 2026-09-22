# ZGL Dashboard

ZGL 团队组织活跃看板系统：IC 卡签到、工作规划与进度跟踪、预约广场、OKR 与项目管理一体化。采用端侧-服务端分离架构。

## 功能模块

| 模块 | 说明 |
|------|------|
| IC 卡签到 | 刷卡即签到，Web 端实时展示 |
| 成员与卡片管理 | 登录即建档；支持一人多卡；未绑定卡刷卡后可在首页认领；本人可自助解绑卡片、维护信息 |
| 考勤统计 | 15/60/180 天及上月出勤汇总、柱状图、56 天热力图、单人签到明细 |
| 工作规划跟踪 | 每周计划、每日日报、AI 月度摘要与建议 |
| OKR 管理 | 按「周期 → 目标 → 关键结果 → 里程碑」组织，进度跟踪与编辑审批流转 |
| 预约广场 | 发布长期/一次性空闲时段，成员预约/取消 |
| 项目管理（ZGantt） | 项目、成员、工作组、工作项与项目考勤管理 |
| 消息通知 | 钉钉群机器人每周自动推送团队周报摘要 |

## 架构

```
读卡器(USB) → 树莓派(checkin_usb.py) → HTTP POST → 服务端(Flask) → Web看板
```

- **服务端**：Flask Web 应用，提供看板页面与端侧同步 API
- **端侧** `checkin_usb.py`：树莓派上运行，通过 evdev 读取 USB IC 读卡器，刷卡先写本地 SQLite 再同步到服务端，断网自动补传

## 快速开始

### 1. 创建环境

```bash
conda env create -f environment.yml
conda activate dashboard_app
```

### 2. 配置

```bash
cp config.example.yml .config.yml
# 编辑 .config.yml，填写实际的 secret_key、api_secret、允许的端侧 IP 等
```

### 3. 创建用户列表

```bash
# 创建 .users.txt，每行一个用户名（管理员可在用户名后追加 ,is_admin）
echo "your_username" > .users.txt
```

### 4. 启动服务端

```bash
python app.py       # 直接启动，0.0.0.0:5000
python app.py pre   # 预发环境（本地测试用），端口与库见 .config.yml
```

生产环境建议 gunicorn + systemd 部署.

### 5. 启动端侧（可部署在树莓派上）

```bash
sudo python3 checkin_usb.py
```

常用 systemd / journalctl 命令：

```bash
sudo systemctl start dashboard      # 启动服务
sudo systemctl stop dashboard       # 停止服务
sudo systemctl restart dashboard    # 重启服务
sudo systemctl enable dashboard     # 开机自启
sudo systemctl disable dashboard    # 取消开机自启
sudo systemctl status dashboard     # 查看服务状态

sudo systemctl daemon-reload        # 修改 dashboard.service 后必须重新加载，否则不生效

sudo journalctl -u dashboard -f     # 实时滚动查看新日志
sudo journalctl -u dashboard --since "1 hour ago"   # 查看最近 1 小时内的日志
```

## 登录认证

- Web 页面：用户名来自 `.users.txt`，密码规则见源码
- 端侧 API：HMAC-SHA256 按日轮换 token（`Authorization: Bearer <token>`）
- 签到 API 额外要求端侧 IP 白名单（只允许树莓派等端侧设备）

## 更多文档

面向开发者的代码结构、数据库 schema 与实现细节见 [CLAUDE.md](CLAUDE.md)。
