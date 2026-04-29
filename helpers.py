from datetime import datetime, timedelta


def compute_week_key(dt):
    """
    计算提交归属的 ISO week key。
    周六/周日 -> 归属下周
    周一上午(<=12点) -> 归属当周
    其他时间 -> 拒绝（返回 None）
    """
    weekday = dt.weekday()  # 0=Mon, 5=Sat, 6=Sun
    hour = dt.hour
    if weekday == 5 or weekday == 6:  # 周六、周日 -> 下周
        next_mon = dt + timedelta(days=(7 - weekday))
        return next_mon.strftime('%Y%W')
    elif weekday == 0 and hour <= 12:  # 周一上午 -> 当周
        return dt.strftime('%Y%W')
    else:
        return None


def compute_upcoming_instances(slot, n=4):
    """根据 slot 类型计算接下来 n 个可用实例日期"""
    today = datetime.now().date()
    if slot['slot_type'] == 'one_time':
        if slot['specific_date'] and slot['specific_date'] >= today.strftime('%Y-%m-%d'):
            return [slot['specific_date']]
        return []
    else:  # recurring
        target_dow = slot['day_of_week']
        days_ahead = (target_dow - today.weekday()) % 7
        if days_ahead == 0 and datetime.now().hour >= slot['start_hour']:
            days_ahead = 7
        first_date = today + timedelta(days=days_ahead)
        return [(first_date + timedelta(weeks=i)).strftime('%Y-%m-%d') for i in range(n)]