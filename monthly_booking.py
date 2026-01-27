"""
月场预定模块
负责处理月场预定任务的创建、执行和管理
"""
import calendar
import datetime
import threading
import time
import uuid
import requests
import json
from typing import List, Dict, Any
from core import redis_client, add_log, check_token_validity, send_email_notification

# 场地ID映射（1-16号场地）
VENUE_ID_MAP = {
    "1": "511508061201884",
    "2": "511589859434885",
    "3": "511687124682886",
    "4": "511764318926887",
    "5": "511839951512888",
    "6": "511942956511889",
    "7": "512037093039890",
    "8": "512160523250891",
    "9": "512288707374892",
    "10": "512382636613893",
    "11": "512467244428894",
    "12": "512536644146895",
    "13": "512624841178896",
    "14": "512719988472897",
    "15": "512805783789898",
    "16": "512885983484899",
}

# 月场任务管理
MONTHLY_TASKS = {}  # {task_id: task_data}
MONTHLY_TASK_LOCK = threading.Lock()

def month_weekday_timestamps(year: int, month: int, weekday: int) -> dict:
    """
    返回字典，键为当月所有指定"周几"对应的日期字符串 "YYYY-MM-DD"，值为该日 00:00:00（UTC+8）的毫秒级时间戳。
    
    参数：
      year: 年份，例如 2026
      month: 月份 1–12，例如 2
      weekday: 周几，1=Monday … 7=Sunday
    """
    if not 1 <= weekday <= 7:
        raise ValueError("weekday 必须在 1 到 7 之间，1=Monday…7=Sunday")
    
    wd_index = weekday - 1
    weeks = calendar.monthcalendar(year, month)
    
    tz_utc8 = datetime.timezone(datetime.timedelta(hours=8))
    
    result = {}
    for week in weeks:
        day = week[wd_index]
        if day:
            dt = datetime.datetime(year, month, day, 0, 0, 0, tzinfo=tz_utc8)
            ms = int(dt.timestamp() * 1000)
            key = f"{year:04d}-{month:02d}-{day:02d}"
            result[key] = ms
    
    return result

def send_monthly_booking_request(token: str, user_id: int, year: int, month: int, 
                                 weekday: int, start_time: str, end_time: str, 
                                 venue_id: str) -> tuple:
    """
    发送月场预定请求
    
    返回：(success: bool, message: str, response_data: dict)
    """
    url = "https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/apply"
    
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": "https://venue.spe.scut.edu.cn",
        "referer": "https://venue.spe.scut.edu.cn/vb-user/booking",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # 计算该月指定周几的所有日期时间戳
    timestamps = month_weekday_timestamps(year, month, weekday)
    receipts = len(timestamps) * 40  # 每次40元
    last_value = list(timestamps.values())[-1] if timestamps else None
    
    if not last_value:
        return False, "无法计算目标日期", {}
    
    payload = {
        "userId": user_id,
        "receipts": receipts,
        "buyerSource": 4,
        "stadiumId": 1,
        "mode": "week",
        "rentals": [{
            "belongDate": last_value,
            "week": weekday,
            "start": start_time,
            "end": end_time,
            "venueId": int(venue_id)
        }]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response_data = response.json()
        
        if response.status_code == 200 and response_data.get('code') == 1:
            return True, "预定成功", response_data
        else:
            error_msg = response_data.get('msg', '未知错误')
            return False, error_msg, response_data
            
    except Exception as e:
        return False, f"请求异常: {str(e)}", {}

def execute_monthly_booking_task(task_id: str):
    """
    执行月场预定任务的后台线程
    """
    with MONTHLY_TASK_LOCK:
        task = MONTHLY_TASKS.get(task_id)
        if not task:
            return
    
    try:
        username = task['username']
        token = task['token']
        user_id = task['user_id']
        email = task['email']
        target_year = task['target_year']
        target_month = task['target_month']
        weekday = task['weekday']
        start_time = task['start_time']
        end_time = task['end_time']
        venue_ids = task['venue_ids']
        
        add_log(f"📅 [月场预定] {username} 任务已启动，目标: {target_year}年{target_month}月 周{weekday}")
        
        # 计算执行时间：目标月份的前一个月最后一天
        # 例如：目标2月，则执行时间为1月31日 17:59:50
        first_day_of_target_month = datetime.date(target_year, target_month, 1)
        last_day_of_prev_month = first_day_of_target_month - datetime.timedelta(days=1)
        
        target_date = datetime.datetime(
            last_day_of_prev_month.year, 
            last_day_of_prev_month.month, 
            last_day_of_prev_month.day, 
            17, 59, 50
        )
        
        # 更新任务状态
        with MONTHLY_TASK_LOCK:
            task['status'] = 'waiting'
            task['target_time'] = target_date.strftime("%Y-%m-%d %H:%M:%S")
            save_monthly_task_to_redis(task_id, task)
        
        add_log(f"⏰ [月场预定] 等待目标时间: {target_date.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 等待到目标时间
        while True:
            now = datetime.datetime.now()
            diff = (target_date - now).total_seconds()
            
            if diff <= 0:
                break
            
            # 智能休眠
            if diff > 5:
                time.sleep(min(diff - 2, 60))
            elif diff > 0.5:
                time.sleep(0.1)
            else:
                pass  # 最后0.5秒空转
        
        add_log(f"🔥 [月场预定] {username} 时间到！开始抢场！")
        
        # 更新状态为执行中
        with MONTHLY_TASK_LOCK:
            task['status'] = 'running'
            save_monthly_task_to_redis(task_id, task)
        
        # 检查 Token 有效性
        if not check_token_validity(token):
            add_log(f"❌ [月场预定] {username} Token 已失效，任务终止")
            with MONTHLY_TASK_LOCK:
                task['status'] = 'failed'
                task['error'] = 'Token已失效'
                save_monthly_task_to_redis(task_id, task)
            
            # 发送失败邮件
            if email:
                send_email_notification(
                    email, username,
                    f"⚠️ 月场预定失败\n\nToken已失效，请重新登录后再创建任务。"
                )
            return
        
        # 高频提交阶段（17:59:50 - 18:00:10，共20秒）
        end_time_stamp = time.time() + 20
        success_venues = []
        failed_venues = []
        
        while time.time() < end_time_stamp:
            threads = []
            results = {}
            
            def submit_venue(vid):
                success, msg, data = send_monthly_booking_request(
                    token, user_id, target_year, target_month, 
                    weekday, start_time, end_time, vid
                )
                results[vid] = (success, msg, data)
            
            # 并发提交所有场地
            for vid in venue_ids:
                t = threading.Thread(target=submit_venue, args=(vid,))
                threads.append(t)
                t.start()
            
            # 等待所有线程完成
            for t in threads:
                t.join(timeout=0.5)
            
            # 检查结果
            for vid, (success, msg, data) in results.items():
                if success and vid not in success_venues:
                    success_venues.append(vid)
                    add_log(f"✅ [月场预定] {username} 场地{vid}预定成功！")
                elif not success and vid not in failed_venues and vid not in success_venues:
                    failed_venues.append(vid)
            
            # 如果所有场地都成功了，提前结束
            if len(success_venues) == len(venue_ids):
                break
            
            time.sleep(0.1)  # 每100ms一轮
        
        # 任务完成，更新状态
        final_status = 'success' if success_venues else 'failed'
        with MONTHLY_TASK_LOCK:
            task['status'] = final_status
            task['success_venues'] = success_venues
            task['failed_venues'] = failed_venues
            task['completed_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_monthly_task_to_redis(task_id, task)
        
        # 发送邮件通知
        if email:
            if success_venues:
                venue_list = "、".join([f"场地{v}" for v in success_venues])
                order_info = (
                    f"✅ 月场预定成功！\n\n"
                    f"时间：{target_year}年{target_month}月 周{weekday}\n"
                    f"时段：{start_time}-{end_time}\n"
                    f"场地：{venue_list}\n\n"
                    f"请登录系统查看订单详情。"
                )
                send_email_notification(email, username, order_info)
            else:
                order_info = (
                    f"❌ 月场预定失败\n\n"
                    f"目标：{target_year}年{target_month}月 周{weekday} {start_time}-{end_time}\n"
                    f"场地：{', '.join(venue_ids)}\n\n"
                    f"可能原因：场地已被预定或系统繁忙"
                )
                send_email_notification(email, username, order_info)
        
        add_log(f"🏁 [月场预定] {username} 任务完成 - 成功:{len(success_venues)} 失败:{len(failed_venues)}")
        
    except Exception as e:
        add_log(f"❌ [月场预定] 任务执行异常: {e}")
        import traceback
        traceback.print_exc()
        
        with MONTHLY_TASK_LOCK:
            task['status'] = 'error'
            task['error'] = str(e)
            save_monthly_task_to_redis(task_id, task)

def create_monthly_booking_task(username: str, token: str, user_id: int, email: str,
                                target_year: int, target_month: int, weekday: int,
                                start_time: str, end_time: str, venue_ids: List[str]) -> str:
    """
    创建月场预定任务
    
    返回 task_id
    """
    task_id = str(uuid.uuid4())
    
    task = {
        'task_id': task_id,
        'username': username,
        'token': token,
        'user_id': user_id,
        'email': email,
        'target_year': target_year,
        'target_month': target_month,
        'weekday': weekday,
        'start_time': start_time,
        'end_time': end_time,
        'venue_ids': venue_ids,
        'status': 'pending',
        'created_at': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'success_venues': [],
        'failed_venues': []
    }
    
    with MONTHLY_TASK_LOCK:
        MONTHLY_TASKS[task_id] = task
        save_monthly_task_to_redis(task_id, task)
    
    # 启动后台执行线程
    t = threading.Thread(target=execute_monthly_booking_task, args=(task_id,), daemon=True)
    t.start()
    
    return task_id

def get_monthly_tasks(username: str = None) -> List[Dict]:
    """
    获取月场任务列表
    """
    with MONTHLY_TASK_LOCK:
        tasks = list(MONTHLY_TASKS.values())
    
    if username:
        tasks = [t for t in tasks if t['username'] == username]
    
    # 按创建时间倒序
    tasks.sort(key=lambda x: x['created_at'], reverse=True)
    return tasks

def cancel_monthly_task(task_id: str, username: str) -> bool:
    """
    取消月场任务（仅能取消 pending/waiting 状态的任务）
    """
    with MONTHLY_TASK_LOCK:
        task = MONTHLY_TASKS.get(task_id)
        if not task:
            return False
        
        if task['username'] != username:
            return False
        
        if task['status'] not in ['pending', 'waiting']:
            return False
        
        task['status'] = 'cancelled'
        task['cancelled_at'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_monthly_task_to_redis(task_id, task)
    
    return True

def save_monthly_task_to_redis(task_id: str, task: Dict):
    """保存月场任务到 Redis"""
    try:
        key = f"scut_order:monthly_tasks:{task_id}"
        redis_client.set(key, json.dumps(task, ensure_ascii=False), ex=90*24*3600)  # 保存90天
    except:
        pass

def load_monthly_tasks_from_redis():
    """从 Redis 加载所有月场任务"""
    try:
        keys = redis_client.keys("scut_order:monthly_tasks:*")
        for key in keys:
            data = redis_client.get(key)
            if data:
                task = json.loads(data)
                task_id = task['task_id']
                
                with MONTHLY_TASK_LOCK:
                    MONTHLY_TASKS[task_id] = task
                
                # 如果任务处于等待或挂起状态，恢复执行线程
                if task['status'] in ['pending', 'waiting']:
                    print(f"Resuming monthly task: {task_id}")
                    t = threading.Thread(target=execute_monthly_booking_task, args=(task_id,), daemon=True)
                    t.start()
    except Exception as e:
        print(f"Error loading monthly tasks: {e}")

# 服务启动时加载历史任务
load_monthly_tasks_from_redis()
