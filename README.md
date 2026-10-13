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

## API 用量页面

DeepSeek 和智谱的 Coding Plan Key 配置在项目根目录的私有 `.coding_plan_keys.yml`，格式见 `coding_plan_keys.example.yml`。当前预留 DeepSeek 一个、智谱三个独立槽位；在对应 `secret` 填入完整 Key 后重启 Dashboard 才可领取。每个 Key 的 `id` 应保持稳定，以便保留其使用人数。页面默认仅显示脱敏值；登录用户点击“＋ 使用并复制”后登记为使用者并取用完整 Key，同一人重复点击只计一次，也可“停止使用”。人数是成员自行登记的数量，不是服务商统计的实际调用人数。登记保存在 `llm_usage_sync.mirror_db_path` 指向的用量数据库的 `coding_plan_claims` 表中，每日用量同步不会清除该表；完整 Key 只在私有配置文件中。部署时应保护该文件并使用 HTTPS 提供 Dashboard 页面。

`/api-usage` 在登录后固定展示百炼、DeepSeek、智谱、Kimi 四张月度用量卡片，第二板块按成员英文名展示所选月份的 Token 总量，不按 Key 展示。点击平台名或成员名可看对应月份最后七天（当月截至今天）按模型分色的每日 Token 柱状图。用量卡片不估算实际费用；Kimi 尚未接入时显示零用量和“待接入”。页面从独立的本地 SQLite 镜像库读取数据，不在用户打开页面时请求 Proxy。转发服务的厂商 API Key 和原始数据库保留在阿里云 ECS；百炼只在 Proxy 中配置一条共享 Key，成员使用各自的 `sk-name`。Proxy 和镜像库均长期保存“人员 × 模型”和“Key × 模型”两套月度汇总，因此共享 Key 仍能追溯到具体使用人。Dashboard 的用量镜像库只保存统计字段、Key ID 和显示标签。Coding Plan Key 则按上述独立私有配置提供，不经过 Proxy。原有 DeepSeek 月度摘要仍按原配置运行。

平台卡片的模型介绍、参考价格文字及官方计费链接保存在项目根目录的 `platform_models.yml`。修改 `title`、`lines`、`note` 或 `pricing_url` 后刷新页面即可生效，无需重启。历史月度汇总若缺少模型信息，会以“历史未细分”保留 Token 总量；当逐请求明细完整且与月汇总的各项指标一致时，同步会按实际模型回填。

在 Dashboard 私有 `.config.yml` 的 `server.llm_usage_sync` 中设置 `enabled: true`、`base_url`、Proxy 的 Dashboard 专用 `dashboard_token`、`mirror_db_path` 和 `sync_start_month`。`dashboard_token` 只用于只读统计接口，不是厂商 API Key。`mirror_db_path` 必须使用与原 `db_path` 不同的绝对路径，并且 Dashboard 服务账号对其父目录有写权限。预发环境应配置自己的 `pre.llm_usage_sync.mirror_db_path`。生产建议使用 HTTPS，避免服务令牌在公网传输时暴露。

配置完成后启动 `python app.py`：应用会异步执行一次初始同步，之后在每天北京时间 00:00、03:00、06:00、09:00、12:00、15:00、18:00、21:00 自动拉取。管理员也可在 API 用量页面点击「立即更新」；在 `.users.txt` 中给用户名追加 `,is_admin` 并重启应用即可授予管理员权限。无需为用量同步安装额外的 service 或 timer。Dashboard 进程需要持续运行；若现有部署使用 Gunicorn，多 worker 会各自启动内置调度器，因此这套方式应使用单 worker。应用停机期间的整点任务不会自动补跑，重新启动时的初始同步会从本地游标继续。

请先升级 ECS 上的 Proxy，再升级 Dashboard。首次使用增量接口时，Dashboard 会拉取起始月份以来的月汇总及 Proxy 尚保留的逐请求明细；之后只查询发生变化的月份，并按 `(updated_at, id)` 游标分页拉取新增或状态更新的明细。游标与镜像数据保存在同一个 SQLite 数据库中，每页提交后可断点续传。为防止同一秒内完成的请求漏掉状态变化，每次会重读上次游标所在秒，按请求 ID 去重更新。每次完整同步成功后，Dashboard 会清理 B 端过期明细，保留当前自然月及之前两个自然月（例如 10 月保留 8 月 1 日至今，12 月保留 10 月 1 日至今）；三张月汇总表仍长期保留。Proxy 尚未提供新接口（HTTP 404）时自动沿用原全量同步方式；升级 Proxy 后，下次同步自动切换增量。

需要单独验证 Proxy 连通性或手动补拉时，可以运行：

```bash
python sync_usage.py
```

同步日志随现有 Dashboard 进程输出，可用 `journalctl -u dashboard` 查看；页面仍显示上次成功保存的数据，并标记同步状态。Proxy 的历史明细仅保留约三个月，月度汇总长期保留，因此首次同步应尽早执行。
