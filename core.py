import os, time, datetime, random, re, subprocess, threading, requests, json, base64, smtplib, sys, shutil, atexit
try:
    from config import SMTP_SERVER, SMTP_PORT, SMTP_SENDER, SMTP_PASSWORD
except ImportError:
    # 配置文件不存在时使用环境变量或默认值
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
    SMTP_SENDER = os.getenv("SMTP_SENDER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
from email.mime.text import MIMEText
from email.header import Header
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import redis

# --- 配置 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
MEMORY_LOGS = []  # 内存日志备用
MEMORY_LOG_LOCK = threading.Lock()


# 自动检测 chromedriver 路径
def get_chromedriver_path():
    # 优先使用重命名后的 chromedriver-new，防止被旧系统误杀
    for p in ["/usr/bin/chromedriver-new", "/usr/local/bin/chromedriver-new", "chromedriver-new"]:
        try:
            if subprocess.run([p, "--version"], capture_output=True).returncode == 0:
                return p
        except: pass
    
    # 备选回退
    for p in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver", "chromedriver"]:
        try:
            if subprocess.run([p, "--version"], capture_output=True).returncode == 0:
                return p
        except: pass
    return "chromedriver"

DRIVER_PATH = get_chromedriver_path()
BROWSER_SEMAPHORE = threading.Semaphore(int(os.environ.get("BROWSER_LIMIT", 2)))
ACTIVE_DRIVER_PIDS = set()
PID_LOCK = threading.Lock()
PENDING_DRIVERS = {} # 存储等待 2FA 的 driver
DRIVER_MAP_LOCK = threading.Lock()

# --- 会话管理 (新增，用于自动救援) ---
USER_SESSIONS = {}
SESSION_LOCK = threading.Lock()
SESSION_FILE = "sessions.json"

def load_sessions_from_file():
    """从文件加载 Session 数据"""
    global USER_SESSIONS
    import os
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                with SESSION_LOCK:
                    USER_SESSIONS = data
                add_log(f"💾 已加载 {len(USER_SESSIONS)} 个缓存 Session")
        except Exception as e:
            add_log(f"⚠️ Session 文件加载失败: {e}")

def save_sessions_to_file():
    """保存 Session 数据到文件"""
    try:
        with SESSION_LOCK:
            data = USER_SESSIONS.copy()
        with open(SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log(f"⚠️ Session 文件保存失败: {e}")

def save_session_to_redis(username, session_data):
    """保存 SESSION 到 Redis（供 Celery worker 访问）"""
    try:
        # 简化 cookies：移除不必要的字段
        simplified_data = session_data.copy()
        if 'cookies' in simplified_data and isinstance(simplified_data['cookies'], dict):
            # 移除 my_client_ticket
            cookies_copy = simplified_data['cookies'].copy()
            cookies_copy.pop('my_client_ticket', None)
            simplified_data['cookies'] = cookies_copy
        
        redis_client.set(
            f"user_session:{username}",
            json.dumps(simplified_data),
            ex=86400  # 24小时过期
        )
    except Exception as e:
        add_log(f"⚠️ Redis SESSION 保存失败: {e}")

def get_session_from_redis(username):
    """从 Redis 获取 SESSION"""
    try:
        data = redis_client.get(f"user_session:{username}")
        if data:
            if isinstance(data, bytes):
                return json.loads(data.decode('utf-8'))
            return json.loads(data)
        return None
    except Exception as e:
        add_log(f"⚠️ Redis SESSION 读取失败: {e}")
        return None

# --- 任务持久化 ---
def save_task_to_redis(task_id, task_data):
    """保存任务到 Redis"""
    try:
        # task_data 可能包含不可序列化的对象(如 Event, Thread)，需过滤
        serializable = {
            "type": task_data.get("type"),
            "status": task_data.get("status"),
            "info": task_data.get("info"),
            "username": task_data.get("username"),
            # 保存创建参数以便可能的恢复
            "params": task_data.get("params", {}) 
        }
        redis_client.hset("scut_order:tasks", task_id, json.dumps(serializable))
    except Exception as e:
        print(f"Redis Task Save Error: {e}")

def remove_task_from_redis(task_id):
    """从 Redis 移除任务"""
    try:
        redis_client.hdel("scut_order:tasks", task_id)
    except: pass

def load_all_tasks_from_redis():
    """从 Redis 加载所有任务 (纯数据，不含线程)"""
    try:
        raw = redis_client.hgetall("scut_order:tasks")
        tasks = {}
        for k, v in raw.items():
            tasks[k] = json.loads(v)
        return tasks
    except: return {}

def add_log(msg, username=None):
    """
    添加日志，支持用户隔离
    - 如果指定 username，日志写入 scut_order:logs:{username}
    - 同时写入全局日志 scut_order:logs:global（用于管理员查看）
    """
    # 更激进的去重：如果是同样的文字，30秒内不重复
    try:
        dedup_key = f"scut_order:last_log:{username}" if username else "scut_order:last_log:global"
        last_log = redis_client.get(dedup_key)
        if last_log == msg:
            last_time = redis_client.get(f"{dedup_key}_time")
            if last_time and time.time() - float(last_time) < 30:
                return
    except: pass
    
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{ts}] {msg}"
    print(full_msg)
    try:
        dedup_key = f"scut_order:last_log:{username}" if username else "scut_order:last_log:global"
        redis_client.set(dedup_key, msg, ex=60)
        redis_client.set(f"{dedup_key}_time", str(time.time()), ex=60)
        
        # 写入用户专属日志
        if username:
            user_log_key = f"scut_order:logs:{username}"
            redis_client.lpush(user_log_key, full_msg)
            redis_client.ltrim(user_log_key, 0, 199)
        
        # 同时写入全局日志
        redis_client.lpush("scut_order:logs:global", full_msg)
        redis_client.ltrim("scut_order:logs:global", 0, 499)
    except Exception as e:
        # Redis 写入失败，降级到内存
        try:
            with MEMORY_LOG_LOCK:
                # 尽量保持结构一致
                MEMORY_LOGS.insert(0, full_msg)
                if len(MEMORY_LOGS) > 200:
                    MEMORY_LOGS.pop()
        except: pass
        print(f"Redis Write Error: {e}")

def check_whitelist(username):
    path = "allowed_users.txt"
    if not os.path.exists(path): return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            allowed = {l.split('#')[0].strip() for l in f if l.split('#')[0].strip()}
            return str(username).strip() in allowed
    except: return True

def send_email_notification(receiver, account_name, order_info):
    """ 发送邮件通知 """
    if not receiver:
        return

    smtp_server = SMTP_SERVER
    smtp_port = SMTP_PORT
    sender = SMTP_SENDER
    password = SMTP_PASSWORD

    subject = f'🏸 订场成功提醒：账号 {account_name} 需要付款'

    content = f"""账号 [{account_name}] 抢到场地！

订单详情：
{order_info}

请务必在10分钟内登录系统完成支付，否则订单将自动取消。
(本邮件由华工羽毛球订场助手自动发送)"""

    message = MIMEText(content, 'plain', 'utf-8')
    message['From'] = sender
    message['To'] = receiver
    message['Subject'] = Header(subject, 'utf-8')

    try:
        smtp_obj = smtplib.SMTP_SSL(smtp_server, smtp_port)
        smtp_obj.login(sender, password)
        smtp_obj.sendmail(sender, [receiver], message.as_string())
        add_log(f"📧 邮件通知已发送至 -> {receiver}")
    except Exception as e:
        add_log(f"❌ 邮件发送失败: {e}")

def send_lock_failed_email(receiver, account_name, venue_name, fail_reason="未知原因"):
    """ 发送锁场失败/掉单通知 """
    if not receiver:
        return

    smtp_server = SMTP_SERVER
    smtp_port = SMTP_PORT
    sender = SMTP_SENDER
    password = SMTP_PASSWORD

    subject = f'⚠️ 锁场失败警告：账号 {account_name} 场地已丢失'

    content = f"""账号 [{account_name}] 锁场模式异常退出！

目标场地：{venue_name}
失败原因：{fail_reason}

系统尝试在60秒内连续续订失败，场地可能已被他人抢走或系统限制。
锁场模式已自动停止，请人工检查。
(本邮件由华工羽毛球订场助手自动发送)"""

    message = MIMEText(content, 'plain', 'utf-8')
    message['From'] = sender
    message['To'] = receiver
    message['Subject'] = Header(subject, 'utf-8')

    try:
        smtp_obj = smtplib.SMTP_SSL(smtp_server, smtp_port)
        smtp_obj.login(sender, password)
        smtp_obj.sendmail(sender, [receiver], message.as_string())
        add_log(f"📧 失败报警邮件已发送至 -> {receiver}")
    except Exception as e:
        add_log(f"❌ 邮件发送失败: {e}")

def kill_zombie_processes():
    """ 
    强制清理所有相关的残留进程
    """
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"], capture_output=True, check=False)
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], capture_output=True, check=False)
        else:
            subprocess.run(["pkill", "-9", "chromedriver"], capture_output=True, check=False)
            subprocess.run(["pkill", "-9", "chrome"], capture_output=True, check=False)
        # add_log("🧹 已执行僵尸进程强力清理")
    except Exception:
        pass  # 静默处理，不打印日志

def process_health_check():
    """
    进程健康巡检：主动发现并清理不属于当前活跃列表的残留进程
    支持 Windows 和 Linux
    """
    try:
        if sys.platform == "win32":
            output = subprocess.check_output('tasklist /FI "IMAGENAME eq chromedriver.exe" /FO CSV /NH', shell=True).decode('gbk', errors='ignore')
            lines = [l.strip() for l in output.strip().split('\n') if l.strip()]
            for line in lines:
                if 'chromedriver.exe' in line:
                    parts = line.split(',')
                    if len(parts) > 1:
                        try:
                            pid = int(parts[1].strip('"'))
                            with PID_LOCK:
                                if pid not in ACTIVE_DRIVER_PIDS:
                                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
                        except ValueError:
                            pass
        else:
            # Linux: 使用 pgrep 查找 chromedriver 进程
            try:
                output = subprocess.check_output(['pgrep', '-f', 'chromedriver'], text=True)
                pids = [int(p.strip()) for p in output.strip().split('\n') if p.strip()]
                for pid in pids:
                    with PID_LOCK:
                        if pid not in ACTIVE_DRIVER_PIDS:
                            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            except subprocess.CalledProcessError:
                pass  # 没有找到进程，正常情况
    except Exception:
        pass


# 定期健康检查线程
_health_check_thread = None
_health_check_stop = threading.Event()

def _health_check_daemon():
    """后台线程：每 5 分钟执行一次进程健康检查"""
    while not _health_check_stop.is_set():
        _health_check_stop.wait(timeout=300)  # 5 分钟
        if not _health_check_stop.is_set():
            process_health_check()

def start_health_check_daemon():
    """启动后台健康检查线程（幂等，可多次调用）"""
    global _health_check_thread
    if _health_check_thread is None or not _health_check_thread.is_alive():
        _health_check_stop.clear()
        _health_check_thread = threading.Thread(target=_health_check_daemon, daemon=True, name="HealthCheckDaemon")
        _health_check_thread.start()

def stop_health_check_daemon():
    """停止后台健康检查线程"""
    _health_check_stop.set()


# --- Session 自动保活 (Keep-Alive) ---
_auto_refresh_thread = None
_auto_refresh_stop = threading.Event()

def _auto_refresh_daemon():
    """后台线程：定期主动刷新 Session，防止 Cookie 过期"""
    while not _auto_refresh_stop.is_set():
        # 每 60 秒检查一次
        _auto_refresh_stop.wait(60)
        if _auto_refresh_stop.is_set(): break
        
        try:
            now = time.time()
            users_to_refresh = []
            
            with SESSION_LOCK:
                # 复制键列表，避免迭代时修改
                for username, session in list(USER_SESSIONS.items()):
                    last_up = session.get('last_updated', 0)
                    # 默认策略：超过 45 分钟未更新 -> 触发主动重登
                    # 只有保存了密码的用户才能自动续期
                    if now - last_up > 2700 and session.get('password'):
                        users_to_refresh.append((username, session.get('password')))
            
            for u, p in users_to_refresh:
                # 检查白名单防止滥用
                if not check_whitelist(u): continue
                
                add_log(f"⏰ [AutoRefresh] {u} 会话即将过期 (>45m)，执行主动续期...", username=u)
                
                # 复用 deduplicated_login (带并发锁)
                # 注意：这会启动浏览器，消耗资源
                status, res = deduplicated_login(u, p)
                
                if status == "success":
                   # deduplicated_login 内部已经更新了 USER_SESSIONS
                   # 这里只需同步到 Redis (deduplicated_login 只更新了内存)
                   with SESSION_LOCK:
                       if u in USER_SESSIONS:
                           save_session_to_redis(u, USER_SESSIONS[u])
                   add_log(f"✅ [AutoRefresh] {u} 续期成功！Cookie已刷新。", username=u)
                elif status == "need_2fa":
                   add_log(f"⚠️ [AutoRefresh] {u} 续期需要 2FA，放弃自动续期。", username=u)
                else:
                   add_log(f"⚠️ [AutoRefresh] {u} 续期失败: {res}", username=u)
                   
                # 随机间隔，避免并发太高
                time.sleep(random.randint(2, 5))
                
        except Exception as e:
            add_log(f"❌ [AutoRefresh] 守护线程异常: {e}")

def start_auto_refresh_daemon():
    """启动 Session 自动保活线程"""
    global _auto_refresh_thread
    if _auto_refresh_thread is None or not _auto_refresh_thread.is_alive():
        _auto_refresh_stop.clear()
        _auto_refresh_thread = threading.Thread(target=_auto_refresh_daemon, daemon=True, name="SessionGuard")
        _auto_refresh_thread.start()
        add_log("🛡️ Session 自动保活服务已启动 (45m/check)")

def stop_auto_refresh_daemon():
    _auto_refresh_stop.set()


# 注册进程退出时的清理函数
def _cleanup_on_exit():
    """进程退出时清理所有活跃的浏览器进程"""
    stop_health_check_daemon()
    stop_auto_refresh_daemon()
    with PID_LOCK:
        pids_to_kill = list(ACTIVE_DRIVER_PIDS)
    
    for pid in pids_to_kill:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True, check=False)
            else:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=False)
        except Exception:
            pass

atexit.register(_cleanup_on_exit)

def _do_init_browser(selected_ua):
    """
    内部实现：实际启动浏览器的逻辑
    返回 driver 或 None
    """
    global DRIVER_PATH
    
    # 1. 驱动检查 - 优先使用系统常见路径
    if not DRIVER_PATH:
        # 优先检测自定义的 chromedriver-new
        possible_paths = [
            "/usr/bin/chromedriver-new", "/usr/local/bin/chromedriver-new", "chromedriver-new",
            "/usr/bin/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
            "/usr/local/bin/chromedriver",
            "/snap/bin/chromium.chromedriver"
        ]
        for p in possible_paths:
            # 简单检查是否存在
            if os.popen(f"which {p}").read().strip() or os.path.exists(p):
                 DRIVER_PATH = p
                 break

        if not DRIVER_PATH:
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                DRIVER_PATH = ChromeDriverManager().install()
            except: pass

    if not DRIVER_PATH:
        # 最后兜底
        DRIVER_PATH = "chromedriver"

    # 2. 获取并发许可
    acquired = BROWSER_SEMAPHORE.acquire(blocking=True, timeout=30)
    if not acquired:
        add_log("❌ 浏览器并发限制已达上限，请稍后再试")
        return None

    options = webdriver.ChromeOptions()
    if os.environ.get("HEADLESS", "true").lower() != "false":
        options.add_argument("--headless=new")
        
    # 解决服务器环境下的启动问题
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument(f"--user-agent={selected_ua}")
    
    # 设置临时用户数据目录（避免多实例冲突）
    import tempfile
    user_data_dir = tempfile.mkdtemp(prefix="chrome_")
    options.add_argument(f"--user-data-dir={user_data_dir}")
    
    # 隐藏 Selenium 特征
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    # 开启性能日志
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    # 随机窗口大小
    width = random.randint(1024, 1920)
    height = random.randint(768, 1080)
    options.add_argument(f"--window-size={width},{height}")

    try:
        service = Service(executable_path=DRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=options)
        
        # 进一步隐藏
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })
        
        driver.set_page_load_timeout(30)
        
        # 记录 PID 和临时目录
        pid = driver.service.process.pid
        driver._pid = pid
        driver._user_agent = selected_ua  # 保存UA到driver对象
        driver._user_data_dir = user_data_dir  # 保存临时目录用于清理
        with PID_LOCK: ACTIVE_DRIVER_PIDS.add(pid)
        # add_log(f"✅ 浏览器已启动 (PID: {pid}, UA: {selected_ua[:50]}...)")
        
        return driver

    except Exception as e:
        add_log(f"❌ 浏览器启动失败: {e}")
        # 清理临时目录
        if user_data_dir and os.path.exists(user_data_dir):
            shutil.rmtree(user_data_dir, ignore_errors=True)
        try: BROWSER_SEMAPHORE.release()
        except: pass
        return None


def init_browser():
    """ 
    工厂模式：每次调用返回全新的 driver 实例 
    添加随机化指纹（User-Agent, 分辨率）和 Selenium 特征隐藏
    支持失败重试机制
    """
    # 候选 UA 列表
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ]
    selected_ua = random.choice(USER_AGENTS)
    
    # 最多尝试2次
    for attempt in range(2):
        if attempt == 0:
            # add_log("🔧 [Init] 准备初始化浏览器...")
            pass  # 首次尝试，静默启动
        else:
            add_log("🔄 [Init] 第二次尝试启动浏览器...")
            # 重试前清理可能的僵尸进程
            process_health_check()
            time.sleep(1)
        
        driver = _do_init_browser(selected_ua)
        if driver:
            return driver
    
    # 两次都失败，执行强力清理后返回 None
    add_log("❌ 浏览器启动失败（已重试），执行强力清理...")
    kill_zombie_processes()
    return None

def close_driver(driver):
    """安全关闭浏览器，清理相关资源"""
    if not driver: return
    
    pid = getattr(driver, '_pid', None)
    user_data_dir = getattr(driver, '_user_data_dir', None)
    
    try:
        driver.quit()
    except Exception as e:
        add_log(f"⚠️ Driver.quit() 失败: {e}")
        # 强制杀进程作为后备
        if pid:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], 
                                   capture_output=True, check=False)
                else:
                    subprocess.run(["kill", "-9", str(pid)], 
                                   capture_output=True, check=False)
                add_log(f"🗑️ 强制终止进程 PID: {pid}")
            except Exception as kill_err:
                add_log(f"⚠️ 强制杀进程失败: {kill_err}")
    finally:
        # 1. 清理 PID 记录
        if pid:
            with PID_LOCK: 
                ACTIVE_DRIVER_PIDS.discard(pid)
        
        # 2. 清理临时用户数据目录
        if user_data_dir and os.path.exists(user_data_dir):
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
                # add_log(f"🧹 已清理临时目录: {user_data_dir}")
            except Exception as rm_err:
                pass  # 静默处理，避免日志刷屏
        
        # 3. 释放信号量
        try:
            BROWSER_SEMAPHORE.release()
        except Exception:
            pass  # 信号量可能已被释放


def sniff_token(driver, timeout=0.5):
    """ 快速嗅探 Token (非阻塞式，但支持 timeout 轮询) """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            logs = driver.get_log("performance")
            for entry in logs:
                try:
                    message = json.loads(entry["message"])["message"]
                    if message["method"] == "Network.requestWillBeSent":
                        req = message["params"]["request"]
                        headers = req.get("headers", {})
                        auth = None
                        for k, v in headers.items():
                            if k.lower() == "authorization":
                                auth = v
                                break
                        if auth and "Bearer" in auth:
                            return auth.replace("Bearer ", "").strip()
                except:
                    continue
        except:
            pass
        
        # 如果是快速嗅探（timeout很短），不需要 sleep 太多
        if timeout > 1:
            time.sleep(0.5)
        else:
            time.sleep(0.1)
            
    return None

def extract_user_info(token):
    try:
        p = token.split('.')[1]
        d = json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p)%4)))
        ui = d.get("userInfo") or {}
        acc = ui.get("sno") or ui.get("account") or d.get("account")
        return {"userId": d.get("userId") or ui.get("userId"), "account": acc}
    except: return None


def check_and_click_campus_login(driver):
    """ 检测并点击'校内账号登录'按钮 """
    try:
        # 方式1: 查找包含特定文字的按钮或div
        xpath_list = [
            "//button[contains(., '校内账号登录')]",
            "//div[contains(text(), '校内账号登录')]",
            "//span[contains(text(), '校内账号登录')]",
            "//a[contains(text(), '校内账号登录')]",
            "//button[contains(., '校内登录')]",
            "//div[contains(text(), '校内登录')]",
            "//*[contains(@class, 'login') and contains(text(), '校内')]",
        ]
        
        for xpath in xpath_list:
            try:
                elems = driver.find_elements(By.XPATH, xpath)
                for elem in elems:
                    if elem.is_displayed():
                        # add_log(f"🔍 找到按钮: {elem.text[:20] if elem.text else 'no-text'}")
                        try:
                            elem.click()
                        except:
                            driver.execute_script("arguments[0].click();", elem)
                        return True
            except:
                pass

        # 方式2: 备用CSS选择器
        css_selectors = [
            "#root > div > div > div > div > div > div:nth-child(2) > button",
            "button.campus-login",
            "[class*='campus'][class*='login']",
            "button:nth-child(2)",  # 通常是第二个按钮
        ]
        
        for css in css_selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, css)
                if elem.is_displayed():
                    # add_log(f"🔍 通过CSS找到按钮: {css[:30]}")
                    try:
                        elem.click()
                    except:
                        driver.execute_script("arguments[0].click();", elem)
                    return True
            except:
                pass

        # 方式3: 遍历所有按钮，查找包含"校内"或"内"的
        try:
            all_buttons = driver.find_elements(By.TAG_NAME, "button")
            # add_log(f"🔍 页面上共有 {len(all_buttons)} 个按钮")
            for btn in all_buttons:
                btn_text = btn.text.strip() if btn.text else ""
                if btn.is_displayed() and ("校内" in btn_text or "Campus" in btn_text.lower()):
                    add_log(f"🔍 找到匹配按钮: {btn_text}")
                    try:
                        btn.click()
                    except:
                        driver.execute_script("arguments[0].click();", btn)
                    return True
        except:
            pass

    except Exception as e:
        add_log(f"⚠️ 检测校内登录按钮异常: {e}")
    return False


def find_visible_input(driver, selectors):
    """ 在一组选择器中找到第一个可见的输入框 """
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for elem in elems:
                if elem.is_displayed() and elem.is_enabled():
                    return elem
        except:
            pass
    return None


def fill_input_robust(driver, elem, text):
    """ 强力输入：清除 -> 输入 -> JS赋值 -> 触发事件 """
    try:
        # 1. 尝试正常输入
        elem.click()
        elem.clear()
        elem.send_keys(text)

        # 2. 检查是否成功，如果不成功或为空，使用JS强制覆盖
        if elem.get_attribute('value') != text:
            add_log("⚠️ 标准输入失效，尝试 JS 强制赋值...")
            driver.execute_script("""
                arguments[0].value = arguments[1];
                arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
                arguments[0].blur();
            """, elem, text)
        return True
    except Exception as e:
        add_log(f"❌ 输入出错: {e}")
        return False


def click_login_btn(driver):
    """ 智能寻找登录按钮并点击 """
    # 常见的登录按钮选择器
    selectors = [
        "#index_login_btn > input",  # 旧版
        "input[value='登录']",
        "input[value='Log In']",
        "button[type='submit']",
        ".btn-primary",
        "#login-button"
    ]

    # 1. 精确匹配
    for sel in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            for elem in elems:
                if elem.is_displayed():
                    try:
                        elem.click()
                    except:
                        driver.execute_script("arguments[0].click();", elem)
                    return True
        except:
            pass

    # 2. 模糊匹配文字
    try:
        xpath = "//button[contains(., '登录')] | //span[contains(., '登录')]/parent::button"
        elems = driver.find_elements(By.XPATH, xpath)
        for elem in elems:
            if elem.is_displayed():
                elem.click()
                return True
    except:
        pass
    return False

def execute_login_logic(username, password, driver=None):
    """
    执行登录流程。
    返回: (status, result_dict)
    - status: "success", result={"token": "...", "cookies": {...}}
    - status: "need_2fa", result=driver
    - status: "error", result=msg
    """
    if not check_whitelist(username): return "error", "白名单拒绝"
    # add_log(f"🚀 [{username}] 启动智能登录 (60s超时)...")
    
    if not driver:
        driver = init_browser()
        if not driver: return "error", "浏览器启动失败"
    
        if not driver: return "error", "浏览器启动失败"
    
    # add_log(f"🌐 [{username}] 浏览器就绪，正在打开登录页...")
    # 确保打开页面
    if "venue" not in driver.current_url and "sso" not in driver.current_url:
        driver.get("https://venue.spe.scut.edu.cn/vb-user/login")
    # add_log(f"📄 当前页面标题: {driver.title}")

    # 定义可能的账号密码框选择器 (包含 SCUT SSO 的常见ID)
    un_selectors = ["#un", "#username", "#account", "input[name='username']", "input[name='account']"]
    pd_selectors = ["#pd", "#password", "input[name='password']", "input[type='password']"]

    start_time = time.time()
    last_action_time = 0

    # === 智能循环 ===
    while time.time() - start_time < 60:
        # 0. 降低循环频率
        time.sleep(1)

        # 1. 优先嗅探 Token
        token = sniff_token(driver, timeout=0.5)
        if token:
#            add_log(f"🎉 [{username}] 成功获取 Token")
            # --- 关键修改：获取 Cookies ---
            # 稍作等待确保 Cookie 写入
            time.sleep(0.5) 
            cookies = {}
            # 添加重试机制
            for attempt in range(3):
                try:
                    time.sleep(0.3 * (attempt + 1))  # 递增延迟: 0.3s, 0.6s, 0.9s
                    cookies = {c['name']: c['value'] for c in driver.get_cookies()}
                    if cookies:  # 成功获取
                        break
                except Exception as e:
                    if attempt == 2:  # 最后一次尝试失败
                        add_log(f"⚠️ [{username}] Cookie获取失败（重试{attempt+1}次）: {e}")
            
            close_driver(driver)
            
            # --- 获取浏览器使用的UA ---
            user_agent = getattr(driver, '_user_agent', None)
            
            # --- 保存会话信息 (新增) ---
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "token": token,
                    "cookies": cookies,
                    "password": password, # 保存密码用于救援
                    "user_agent": user_agent,  # 保存UA用于续订
                    "last_updated": time.time()
                }
            
            return "success", {"token": token, "cookies": cookies, "user_agent": user_agent}

        # 2. 检测 2FA 界面 (#PM1 是特定的验证码框ID)
        # 直接进入验证码输入模式，让用户填写验证码
        try:
            if len(driver.find_elements(By.ID, "PM1")) > 0:
                add_log(f"🔐 [{username}] 检测到二次验证界面，等待用户输入验证码...")
                with DRIVER_MAP_LOCK: 
                    PENDING_DRIVERS[username] = driver
                return "need_2fa", "等待验证码"
        except Exception as e2fa_err:
            add_log(f"⚠️ [{username}] 2FA检测异常: {e2fa_err}")
            pass


        # 3. 页面动作 (每隔2秒执行一次，避免频繁操作)
        if time.time() - last_action_time < 2:
            continue

        last_action_time = time.time()

        # A. 检测 "校内账号登录" 并点击
        if check_and_click_campus_login(driver):
            # add_log("🔄 正在跳转...")
            continue

        # B. 检测 账号/密码 框
        un_elem = find_visible_input(driver, un_selectors)
        pd_elem = find_visible_input(driver, pd_selectors)

        if un_elem and pd_elem:
            # 获取当前值
            curr_un = un_elem.get_attribute('value')
            curr_pd = pd_elem.get_attribute('value')

            # 填账号
            if curr_un != username:
#                add_log("⌨️  检测到账号框为空或不匹配，正在填充...")
                fill_input_robust(driver, un_elem, username)

            # 填密码
            if curr_pd != password:
#                add_log("⌨️  检测到密码框为空或不匹配，正在填充...")
                fill_input_robust(driver, pd_elem, password)

            # 如果都填好了，点击登录
            # 重新获取值确认
            if un_elem.get_attribute('value') == username and pd_elem.get_attribute('value') == password:
                # add_log("🖱️ 凭证已填充，尝试点击登录按钮...")
                if click_login_btn(driver):
                    # add_log("⏳ 点击成功，等待页面跳转...")
                    time.sleep(2)
            continue

    # 超时
    close_driver(driver)
    return "error", "Login Timeout (60s)"


# --- 登录并发控制器 (新增) ---
class LoginCoordinator:
    def __init__(self):
        self._lock = threading.Lock()
        self._active_logins = {}  # username -> {"event": Event, "result": None}

    def login(self, username, password):
        """
        线程安全的登录入口。
        如果同一个 username 已经在登录中，后续请求会阻塞并共享结果。
        """
        must_login = False
        context = None

        with self._lock:
            if username in self._active_logins:
                # 已经有任务在跑，搭便车
                context = self._active_logins[username]
            else:
                # 我是带头大哥
                must_login = True
                context = {"event": threading.Event(), "result": None}
                self._active_logins[username] = context
        
        if must_login:
            try:
                # 执行真正的登录逻辑
                # add_log(f"⚡ [Coordinator] 线程 {threading.current_thread().name} 获得登录权")
                status, res = execute_login_logic(username, password)
                context["result"] = (status, res)
            except Exception as e:
                context["result"] = ("error", str(e))
            finally:
                # 唤醒等待者
                context["event"].set()
                # 清理记录
                with self._lock:
                    if username in self._active_logins and self._active_logins[username] is context:
                        del self._active_logins[username]
            return context["result"]
        else:
            # 等待者
            # add_log(f"💤 [Coordinator] 线程 {threading.current_thread().name} 等待现有登录任务...")
            context["event"].wait()
            return context["result"]

# 全局单例
LOGIN_COORDINATOR = LoginCoordinator()

def deduplicated_login(username, password):
    """ 包装函数，供外部调用 """
    return LOGIN_COORDINATOR.login(username, password)

def ms_to_dt(ms):
    try: return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except: return ""

def _extract_orders_from_payload(payload):
    data = payload.get("data")
    if isinstance(data, dict):
        for k in ("records", "list", "rows", "items"):
            if isinstance(data.get(k), list): return data[k]
    if isinstance(data, list): return data
    return []

def _normalize_order_records(payload):
    raw_orders = _extract_orders_from_payload(payload)
    records = []
    for o in raw_orders:
        if o.get("projectName") and o.get("projectName") != "羽毛球": continue
        rentals = o.get("rentals") or []
        for r in rentals:
            records.append({
                "fieldName": o.get("projectName") or "羽毛球",
                "belongDate": ms_to_dt(r.get("belongDate"))[:10],
                "startTime": r.get("startTime") or r.get("start") or "",
                "endTime": r.get("endTime") or r.get("end") or "",
                "venueName": r.get("venueName") or r.get("venue") or "",
                "price": float(o.get("receivable") or o.get("receipts") or o.get("amount") or 0),
                "orderNo": o.get("orderNo"),
                "statusDesc": o.get("statusDesc") or o.get("statusName") or o.get("status") or "",
                "createdAt": ms_to_dt(o.get("createdAt"))
            })
    data = payload.get("data")
    if isinstance(data, dict):
        return {"records": records, "page": data.get("page"), "total": data.get("total")}
    return {"records": records}

def fetch_orders_internal(token, status_value, page=1, page_size=10, cookies=None, username=None):
    """
    查询订单列表（四种状态），对齐用户提供的抓包脚本：
    GET https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/page
    参数：page, pageSize, status （status 为单个整数：1/2/3/4）
    """
    url = "https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/page"

    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token}",
        "user-agent": "Mozilla/5.0",
        "origin": "https://venue.spe.scut.edu.cn",
        "referer": "https://venue.spe.scut.edu.cn/vb-user/booking",
    }

    params = {"page": int(page), "pageSize": int(page_size), "status": int(status_value)}

    def _do_request(tok, ck):
        return requests.get(url, headers={**headers, "authorization": f"Bearer {tok}"}, params=params, cookies=ck, timeout=15)

    try:
        # 1) 首次请求
        resp = _do_request(token, cookies)

        # 2) 自动救援：拿到 HTML 说明会话失效/被重定向到登录页
        if resp.status_code == 200 and ("<html" in resp.text.lower() or "doctype html" in resp.text.lower()):
            if username:
                add_log(f"⚠️ [{username}] 查看订单时 Session 失效，触发自动救援.")
                pwd = None
                with SESSION_LOCK:
                    if username in USER_SESSIONS:
                        pwd = USER_SESSIONS[username].get("password")

                if pwd:
                    add_log(f"🔄 正在后台重新登录 {username}.")
                    status, res = execute_login_logic(username, pwd)
                    if status == "success":
                        new_token = res["token"]
                        new_cookies = res["cookies"]

                        # 更新缓存
                        with SESSION_LOCK:
                            if username in USER_SESSIONS:
                                USER_SESSIONS[username]["token"] = new_token
                                USER_SESSIONS[username]["cookies"] = new_cookies
                                USER_SESSIONS[username]["last_updated"] = time.time()

                        # 重试请求
                        resp = _do_request(new_token, new_cookies)
                    else:
                        add_log(f"❌ 救援失败: {res}")
                else:
                    add_log("❌ 无法救援: 缺少保存的密码")

        # 3) 解析响应
        if resp.status_code != 200:
            add_log(f"❌ 订单查询 HTTP {resp.status_code}")
            return None

        payload = resp.json()
        # 兼容 code=1 或 code=200
        if payload.get("code") not in (1, 200) and payload.get("status") not in ("success",):
            # 有些接口会用 msg/状态说明
            return None

        return _normalize_order_records(payload)

    except Exception as e:
        add_log(f"❌ 订单查询异常: {e}")
        return None

def fetch_venue_data(token, date_str, cookies=None, username=None, user_agent=None):
    """
    使用 chaxun.txt 的逻辑进行数据查询，支持 Cookie 和 自动救援
    参数:
        cookies: 必须传入，学校后端同时验证 Token + Cookie
        user_agent: 可选，传入特定UA以保持一致性
    """
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    ts = int(dt.replace(hour=0,minute=0,second=0).timestamp() * 1000)
    url = "https://venue.spe.scut.edu.cn/api/pc/venue/pc/booking"
    
    # 使用传入的UA或默认UA
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "user-agent": ua,
        "origin": "https://venue.spe.scut.edu.cn",
        "referer": "https://venue.spe.scut.edu.cn/vb-user/booking"
    }
    
    payload = {
        "projectId": 3,
        "stadiumId": 1,
        "belongDate": ts,
        "weekday": "",
        "bookingType": "week"
    }

    try:
        # 1. 尝试第一次请求（需要 Token + Cookie 同时验证）
        # print(f"DEBUG: fetch_venue_data calling requests.post... token={token[:10]}...", flush=True)
        resp = requests.post(url, headers=headers, json=payload, cookies=cookies, timeout=8)
        # print(f"DEBUG: fetch_venue_data response: {resp.status_code}", flush=True)
        
        # 2. 核心救援逻辑：检测是否返回了 HTML (登录页)
        # 关键：检查 Content-Type 确保真的是 HTML 页面，避免误判
        content_type = resp.headers.get('Content-Type', '').lower()
        is_html_page = 'text/html' in content_type
        
        # 调试：记录响应类型（临时）
        if username and is_html_page:
            add_log(f"🔍 [DEBUG] 响应 Content-Type: {content_type}, 状态码: {resp.status_code}")
        
        if resp.status_code == 200 and is_html_page:
            if username:
                # 使用锁防止多个请求同时触发救援
                if not hasattr(fetch_venue_data, '_rescue_lock'):
                    fetch_venue_data._rescue_lock = threading.Lock()
                if not hasattr(fetch_venue_data, '_rescuing'):
                    fetch_venue_data._rescuing = {}
                
                with fetch_venue_data._rescue_lock:
                    if fetch_venue_data._rescuing.get(username):
                        # 已有救援在进行，等待结果
                        add_log(f"⏳ [{username}] 等待现有救援完成...")
                        # 返回None让调用方使用缓存或稍后重试
                        return None
                    fetch_venue_data._rescuing[username] = True
                
                try:
                    add_log(f"⚠️ [{username}] Token失效，触发自动救援...")
                    
                    # 优先从 Redis 获取密码（Celery worker 可访问）
                    pwd = None
                    session = get_session_from_redis(username)
                    if session:
                        pwd = session.get('password')
                    else:
                        # 备用：从 USER_SESSIONS 读取
                        with SESSION_LOCK:
                            if username in USER_SESSIONS:
                                pwd = USER_SESSIONS[username].get('password')
                    
                    if pwd:
                        add_log(f"🔄 正在后台重新登录 {username}...")
                        # 重新执行登录 (使用并发控制)
                        status, res = deduplicated_login(username, pwd)
                        
                        if status == "success":
                            new_token = res['token']
                            new_cookies = res['cookies']
                            
                            # 更新全局缓存
                            with SESSION_LOCK:
                                if username in USER_SESSIONS:
                                    USER_SESSIONS[username]['token'] = new_token
                                    USER_SESSIONS[username]['cookies'] = new_cookies
                                    USER_SESSIONS[username]['last_updated'] = time.time()
                                    
                                    # 同时保存到 Redis
                                    save_session_to_redis(username, USER_SESSIONS[username])
                            
                            add_log("✅ 救援成功！使用新凭证重试请求...")
                            # 使用新凭证重试
                            headers["authorization"] = f"Bearer {new_token}"
                            resp = requests.post(url, headers=headers, json=payload, cookies=new_cookies, timeout=8)
                            
                            # 立即解析结果
                            if resp.status_code == 200:
                                res_json = resp.json()
                                if (res_json.get("code") == 1 or res_json.get("code") == 200) and "data" in res_json:
                                    return res_json["data"].get("venueSessionResponses", [])
                        elif status == "need_2fa":
                            # 新增：救援需要 2FA 验证，返回特殊标记让前端处理
                            add_log(f"⚠️ [{username}] 救援需要 2FA 验证，等待用户输入...")
                            return {"__need_rescue_2fa__": True, "username": username}
                        else:
                            add_log(f"❌ 救援失败: {res}")
                    else:
                        add_log("❌ 无法救援: 缺少保存的密码")
                finally:
                    # 清除救援标记
                    with fetch_venue_data._rescue_lock:
                        fetch_venue_data._rescuing[username] = False
        
        # 3. 解析正常响应 (首次成功 或 重试成功)
        if resp.status_code == 200:
            try:
                res_json = resp.json()
                # print(f"DEBUG: fetch_venue_data json: {str(res_json)[:100]}", flush=True)
                if (res_json.get("code") == 1 or res_json.get("code") == 200) and "data" in res_json:
                    return res_json["data"].get("venueSessionResponses", [])
            except:
                pass # JSON 解析失败，或者仍然是 HTML
                
    except Exception as e:
        add_log(f"❌ 数据查询异常: {e}")
    return None
def check_token_validity(token, cookies=None, username=None, user_agent=None):
    """
    检查 Token + Cookie 是否仍可用于获取订场数据（通过 booking 接口探测）。
    注意：学校后端同时验证 Token 和 Cookie，两者都需要有效
    参数:
        user_agent: 传入UA以保持与login时一致
    """
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        sessions = fetch_venue_data(token, today, cookies, username=username, user_agent=user_agent)
        # fetch_venue_data 失败时返回 None
        # print(f"DEBUG: check_token_validity result: {sessions is not None}", flush=True)
        return sessions is not None
    except:
        # print("DEBUG: check_token_validity exception", flush=True)
        return False

def get_booking_params(date_str):
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    tz_utc8 = datetime.timezone(datetime.timedelta(hours=8))
    dt = dt.replace(tzinfo=tz_utc8)
    timestamp = int(dt.timestamp() * 1000)
    weekday = dt.isoweekday()
    return timestamp, weekday

def send_booking_request(token, user_id, date_str, start_time, end_time, venue_id, price=40, stadium_id=1, cookies=None, user_agent=None):
    """
    发送预定请求
    注意：学校后端同时验证 Token + Cookie，必须传入 cookies
    返回: (成功/失败, 消息, 新Cookie字典或None)
    """
    belong_date, week = get_booking_params(date_str)
    url = "https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/apply"

    # 使用传入的UA，如果没有则使用默认值
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "user-agent": ua,
        "origin": "https://venue.spe.scut.edu.cn",
        "referer": "https://venue.spe.scut.edu.cn/vb-user/booking"
    }

    payload = {
        "userId": user_id,
        "receipts": price,
        "buyerSource": 4,
        "stadiumId": stadium_id,
        "mode": "week",
        "rentals": [{
            "belongDate": belong_date,
            "week": week,
            "start": start_time,
            "end": end_time,
            "venueId": int(venue_id)
        }]
    }

    try:
        # 必须同时使用 Token + Cookie（学校后端验证需要）
        resp = requests.post(url, headers=headers, json=payload, cookies=cookies, timeout=5)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 200 or "成功" in str(res_json):
                # 注意:学校后端在续订成功时不返回Set-Cookie头
                # 只能通过定期重新登录来刷新Cookie
                return True, "预定成功", None  # 第三个参数保持None
            return False, res_json.get("msg", str(res_json)), None
        return False, f"HTTP {resp.status_code}", None
    except Exception as e:
        return False, str(e), None

def try_rescue_token(username, reason="unknown"):
    """
    尝试经过自动登录流程救援失效的 Token。
    """
    if not username:
        return False
        
    add_log(f"🚑 [{username}] 触发自动救援 (原因: {reason})...")
    
    pwd = None
    with SESSION_LOCK:
        if username in USER_SESSIONS:
            pwd = USER_SESSIONS[username].get('password')
            
    if not pwd:
        add_log(f"❌ [{username}] 无法救援: 缺少保存的密码")
        return False
        
    add_log(f"🔄 [{username}] 正在后台重新登录...")
    # 这里需要注意避免循环依赖，但 execute_login_logic 已定义在上方，可以直接调用
    status, res = execute_login_logic(username, pwd)
    
    if status == "success":
        new_token = res['token']
        new_cookies = res['cookies']
        
        #execute_login_logic 内部已经更新了 USER_SESSIONS，所以这里不需要再手动更新
        add_log(f"✅ [{username}] 救援成功！")
        return True
    else:
        add_log(f"❌ [{username}] 救援失败: {res}")
        return False
