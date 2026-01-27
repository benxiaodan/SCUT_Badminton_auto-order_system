from celery import Celery
import time, random, os, json
from core import (
    add_log, redis_client, send_booking_request, fetch_venue_data, 
    get_session_from_redis, extract_user_info
)

celery_app = Celery('scut_tasks', broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

def set_task_status(tid, task_type, status, info):
    redis_client.set(f"task_status:{tid}", json.dumps({
        "type": task_type, "status": status, "info": info
    }), ex=86400)

def is_stopped(task_id):
    return redis_client.get(f"task_stop:{task_id}") is not None


@celery_app.task(bind=True)
def lock_task(self, task_id, params):
    """
    锁场保活任务（已预定成功后启动）
    - 只负责每 9 分钟续订一次
    - 复制自 server.py monitor_worker 的续订逻辑
    """
    token = params.get('token')
    date = params.get('date')
    start_time = params.get('startTime')
    end_time = params.get('endTime')
    venue_id = params.get('venueId')
    venue_name = params.get('venueName', f"场地{venue_id}")
    username = params.get('username')
    user_id = params.get('userId')
    price = params.get('price', 40)
    
    info = f"[{username}] {date} {start_time} {venue_name}"
    add_log(f"🔒 [Task {task_id}] 锁场保活已启动")
    set_task_status(task_id, "lock", "已锁场", info)
    
    # 当前凭证（从 Redis 同步，与 server.py 一致）
    current_token = token
    current_cookies = {}
    
    try:
        while not is_stopped(task_id):
            add_log(f"⏸️ [Task {task_id}] 等待 9 分钟后续订...")
            
            # 9分钟等待，每10秒检查一次停止信号
            for _ in range(54):
                if is_stopped(task_id): break
                time.sleep(10)
            
            if is_stopped(task_id): break
            
            # 爆发续订 70 秒（完全复制 server.py 的逻辑）
            add_log(f"⚡ [Task {task_id}] 爆发期开始 (70s)!")
            set_task_status(task_id, "lock", "续订中", info)
            
            burst_start = time.time()
            round_success = False
            
            while time.time() - burst_start < 70:
                if is_stopped(task_id): break
                
                # Token 同步逻辑（与 server.py 一致）
                session = get_session_from_redis(username)
                if session:
                    if session.get('token') and session.get('token') != current_token:
                        current_token = session['token']
                        current_cookies = session.get('cookies', {})
                
                # 发送请求（与 server.py 完全一致的调用方式）
                # DEBUG: 输出调试信息
                add_log(f"🔍 [DEBUG] Token: {current_token[:50]}..." if current_token else "Token: None")
                add_log(f"🔍 [DEBUG] user_id={user_id}, venue_id={venue_id}, price={price}")
                add_log(f"🔍 [DEBUG] cookies keys: {list(current_cookies.keys()) if current_cookies else 'Empty'}")
                
                ok_renew, msg_renew = send_booking_request(
                    current_token, user_id, date, start_time, end_time,
                    venue_id, price, cookies=current_cookies
                )
                
                # DEBUG: 输出结果
                add_log(f"🔍 [DEBUG] 续订结果: ok={ok_renew}, msg={msg_renew}")
                
                if ok_renew:
                    add_log(f"✅ [Task {task_id}] 续订成功!")
                    round_success = True
                    break
                
                time.sleep(0.5)
            
            if not round_success and not is_stopped(task_id):
                add_log(f"⚠️ [Task {task_id}] 本轮续订失败，继续尝试...")
            
            set_task_status(task_id, "lock", "已锁场", info)
    
    except Exception as e:
        add_log(f"❌ [Task {task_id}] 异常: {e}")
    
    add_log(f"⏹️ [Task {task_id}] 锁场任务已停止")
    redis_client.delete(f"task_stop:{task_id}")
    redis_client.delete(f"task_status:{task_id}")
    return "Done"


@celery_app.task(bind=True)
def monitor_task(self, task_id, params):
    """
    自动捡漏任务（扫描空场 -> 预定）
    - 持续扫描指定日期时间的空场
    - 复制自 server.py monitor_worker 的扫描逻辑
    """
    token = params.get('token')
    date = params.get('date')
    start_time = params.get('startTime')
    end_time = params.get('endTime')
    vid = params.get('venueId')  # 可选，如果指定则只监控该场地
    is_lock_mode = params.get('lockMode', False)
    venue_name = params.get('venueName', '')
    username = params.get('username')
    user_id = params.get('userId')
    
    task_type = "lock" if is_lock_mode else "snipe"
    info = f"[{username}] {date} {start_time} {venue_name or '任意场地'}"
    
    add_log(f"👀 [Task {task_id}] 开始监控: {date} {start_time}")
    set_task_status(task_id, task_type, "监控中", info)
    
    # 当前凭证
    current_token = token
    current_cookies = {}
    
    # 同步 token
    session = get_session_from_redis(username)
    if session:
        current_token = session.get('token', token)
        current_cookies = session.get('cookies', {})
    
    try:
        while not is_stopped(task_id):
            # 同步最新凭证（与 server.py 一致）
            session = get_session_from_redis(username)
            if session:
                if session.get('token') and session.get('token') != current_token:
                    current_token = session['token']
                    current_cookies = session.get('cookies', {})
            
            # 扫描场地
            sessions = fetch_venue_data(current_token, date, cookies=current_cookies, username=username)
            
            if is_stopped(task_id): break
            
            # 查找空场（与 server.py 一致的匹配逻辑）
            target = None
            actual_price = 40
            target_vid_str = str(vid) if vid else None
            
            for s in sessions or []:
                try:
                    if s.get('startTime') != start_time:
                        continue
                    if end_time and s.get('endTime') and s.get('endTime') != end_time:
                        continue
                    if int(s.get('availNum', 0)) != 1:
                        continue
                    if target_vid_str and str(s.get('venueId')) != target_vid_str:
                        continue
                    
                    target = s
                    if 'price' in s:
                        actual_price = s['price']
                    break
                except Exception:
                    continue
            
            if target:
                add_log(f"🎉 [Task {task_id}] 发现空闲: {target.get('venueName')}")
                
                # 发送请求（与 server.py 完全一致）
                ok, msg = send_booking_request(
                    current_token, user_id, date, start_time, end_time,
                    target['venueId'], actual_price, cookies=current_cookies
                )
                
                if ok:
                    add_log(f"✅ [Task {task_id}] 预定成功!")
                    
                    if is_lock_mode:
                        # 转换为锁场模式
                        lock_task.delay(task_id + "-L", {
                            **params,
                            'venueId': target['venueId'],
                            'venueName': target.get('venueName'),
                            'price': actual_price,
                            'userId': user_id
                        })
                        add_log(f"🔒 [Task {task_id}] 已启动锁场保活")
                    
                    set_task_status(task_id, task_type, "已完成", info)
                    break
            
            # 随机休眠（与 server.py 一致）
            time.sleep(random.uniform(1.0, 3.0))

    except Exception as e:
        add_log(f"❌ [Task {task_id}] 异常: {e}")
    
    add_log(f"⏹️ [Task {task_id}] 监控任务已停止")
    redis_client.delete(f"task_stop:{task_id}")
    redis_client.delete(f"task_status:{task_id}")
    return "Done"
