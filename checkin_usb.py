import sqlite3
import datetime
import time
import hmac
import hashlib
import threading
import os
import evdev
import requests
import yaml
from evdev import categorize, ecodes

# --- 从 .config.yml 加载配置 ---
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.config.yml')
with open(_config_path) as f:
    _config = yaml.safe_load(f)

# 服务端地址
SERVER_URL = _config['client']['server_url']
# 本地数据库路径
DB_PATH = _config['client']['db_path']
# API 认证密钥（与服务端保持一致）
API_SECRET = _config['client']['api_secret']

# 按键映射表 (Scancode -> Char)
# 该部分依赖读卡器的具体型号和配置可能需要调整，以下是常见的数字键盘扫描码
SCAN_CODES = {
    2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
}

def find_reader():
    """自动寻找读卡器设备"""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    for device in devices:
        if "Xstra" in device.name or "Reader" in device.name:
            return device
    return None

def init_db():
    """初始化本地数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sign_ins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            synced INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    return conn

def local_save(cursor, conn, uid, timestamp):
    """写入本地数据库，synced=0 表示尚未同步"""
    cursor.execute("INSERT INTO sign_ins (uid, timestamp, synced) VALUES (?, ?, 0)", (uid, timestamp))
    conn.commit()

def generate_token():
    """根据当天日期生成 HMAC-SHA256 token"""
    date_str = datetime.datetime.now().strftime('%Y-%m-%d')
    return hmac.new(API_SECRET.encode(), date_str.encode(), hashlib.sha256).hexdigest()

def send_checkin(uid, timestamp, cursor, conn):
    """将刷卡记录发送到服务端，成功则标记 synced=1"""
    try:
        token = generate_token()
        resp = requests.post(
            f'{SERVER_URL}/api/checkin',
            json={'uid': uid, 'timestamp': timestamp},
            headers={'Authorization': f'Bearer {token}'},
            timeout=5
        )
        if resp.status_code == 200 and resp.json().get('success'):
            # 标记最新一条记录为已同步
            cursor.execute("UPDATE sign_ins SET synced=1 WHERE uid=? AND timestamp=? AND synced=0", (uid, timestamp))
            conn.commit()
            print(f"  -> 已同步至服务端")
            return True
        else:
            print(f"  -> 服务端返回异常: {resp.status_code} {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"  -> 同步失败: {e}")
        return False

def retry_sync(cursor, conn):
    """重试7天内未同步的记录，逐条发送到服务端"""
    cursor.execute("SELECT id, uid, timestamp FROM sign_ins WHERE synced=0 AND timestamp >= datetime('now', '-7 days') ORDER BY id")
    unsynced = cursor.fetchall()
    if not unsynced:
        print("无未同步记录")
        return

    print(f"发现 {len(unsynced)} 条未同步记录，开始补同步...")
    success_count = 0
    for row_id, uid, timestamp in unsynced:
        try:
            token = generate_token()
            resp = requests.post(
                f'{SERVER_URL}/api/checkin',
                json={'uid': uid, 'timestamp': timestamp},
                headers={'Authorization': f'Bearer {token}'},
                timeout=5
            )
            if resp.status_code == 200 and resp.json().get('success'):
                cursor.execute("UPDATE sign_ins SET synced=1 WHERE id=?", (row_id,))
                conn.commit()
                success_count += 1
                print(f"  -> 补同步成功: uid={uid}, time={timestamp}")
            else:
                print(f"  -> 补同步失败: uid={uid}, time={timestamp}, 响应: {resp.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"  -> 补同步网络异常: uid={uid}, time={timestamp}, 错误: {e}")

    print(f"补同步完成: 成功 {success_count}/{len(unsynced)} 条")

    # 24小时后再次执行补同步
    timer = threading.Timer(86400, retry_sync, [cursor, conn])
    timer.daemon = True  # 主程序退出时定时器自动终止
    timer.start()
    print("下次补同步将在24小时后自动执行")

def main():
    conn = init_db()
    cursor = conn.cursor()

    # 启动时先尝试补同步历史未同步记录
    retry_sync(cursor, conn)

    device = find_reader()
    if not device:
        print("错误：未找到读卡器，请检查 USB 连接。")
        conn.close()
        return

    print(f"已锁定设备: {device.name}")
    print(f"服务端地址: {SERVER_URL}")
    print("系统运行中，等待刷卡...")

    card_uid = ""

    try:
        # 该部分需要根据读卡器的具体行为进行调整，以下假设读卡器以键盘输入方式输出卡号，并以回车结束
        # 强行截获输入流
        device.grab()
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY:
                key = categorize(event)
                # 仅处理按键按下状态 (keystate 1)
                if key.keystate == 1:
                    # 兼容标准回车(28)和数字键盘回车(96)
                    if key.scancode in [28, 96]:
                        if card_uid:
                            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            print(f"【读卡】卡号: {card_uid} | 时间: {now}")
                            # 先写入本地数据库
                            local_save(cursor, conn, card_uid, now)
                            # 再尝试同步到服务端
                            send_checkin(card_uid, now, cursor, conn)

                            # 清空缓存并防抖
                            card_uid = ""
                            time.sleep(0.5)
                    else:
                        # 累加卡号字符
                        char = SCAN_CODES.get(key.scancode, "")
                        card_uid += char

    except PermissionError:
        print("权限不足！请使用 'sudo python3 checkin_usb.py' 运行。")
    except KeyboardInterrupt:
        print("\n系统已安全退出。")
    finally:
        conn.close()

if __name__ == '__main__':
    main()
