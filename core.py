import os, time, datetime, random, re, subprocess, threading, requests, json, base64, smtplib, sys
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

    smtp_server = "smtp.qq.com"
    smtp_port = 465
    sender = os.environ.get("SMTP_SENDER", "your_email@qq.com")
    password = os.environ.get("SMTP_PASSWORD", "your_smtp_password")  # 授权码

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
    """
    # add_log("🔍 [HealthCheck] 启动进程健检...")
    try:
        if sys.platform == "win32":
            output = subprocess.check_output('tasklist /FI "IMAGENAME eq chromedriver.exe" /FO CSV /NH', shell=True).decode('gbk', errors='ignore')
            lines = [l.strip() for l in output.strip().split('\n') if l.strip()]
            for line in lines:
                if 'chromedriver.exe' in line:
                    parts = line.split(',')
                    if len(parts) > 1:
                        pid = int(parts[1].strip('"'))
                        with PID_LOCK:
                            if pid not in ACTIVE_DRIVER_PIDS:
                                # add_log(f"🗑️ [HealthCheck] 发现孤立进程 {pid}, 正在清理...")
                                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
    except: pass

def init_browser():
    """ 
    工厂模式：每次调用返回全新的 driver 实例 
    添加随机化指纹（User-Agent, 分辨率）和 Selenium 特征隐藏
    """
    add_log("🔧 [Init] 准备初始化浏览器...")
    global DRIVER_PATH
    
    # 候选 UA 列表
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ]
    selected_ua = random.choice(USER_AGENTS)

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
    # add_log("🌐 正在尝试启动浏览器...")
    acquired = BROWSER_SEMAPHORE.acquire(blocking=True, timeout=30)
    if not acquired:
        add_log("❌ 浏览器并发限制已达上限，请稍后再试")
        return None

    options = webdriver.ChromeOptions()
    if os.environ.get("HEADLESS", "true").lower() != "false":
        options.add_argument("--headless=new")
        
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument(f"--user-agent={selected_ua}")
    
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
        
        # 记录 PID
        pid = driver.service.process.pid
        driver._pid = pid
        with PID_LOCK: ACTIVE_DRIVER_PIDS.add(pid)
        add_log(f"✅ 浏览器已启动 (PID: {pid})")
        
        return driver

    except Exception as e:
        add_log(f"❌ 浏览器启动失败: {e}")
        try: BROWSER_SEMAPHORE.release()
        except: pass
        return None

def close_driver(driver):
    if not driver: return
    try:
        pid = getattr(driver, '_pid', None)
        driver.quit()
        if pid:
            with PID_LOCK: ACTIVE_DRIVER_PIDS.discard(pid)
    except: pass
    finally:
        try: BROWSER_SEMAPHORE.release()
        except: pass


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
        # 查找包含特定文字的按钮或div
        xpath = "//button[contains(., '校内账号登录')] | //div[contains(text(), '校内账号登录')]"
        elems = driver.find_elements(By.XPATH, xpath)
        for elem in elems:
            if elem.is_displayed():
#                add_log("👆 点击 '校内账号登录'...")
                try:
                    elem.click()
                except:
                    driver.execute_script("arguments[0].click();", elem)
                return True

        # 备用：特定的CSS
        try:
            elem = driver.find_element(By.CSS_SELECTOR,
                                       "#root > div > div > div > div > div > div:nth-child(2) > button")
            if elem.is_displayed():
#                add_log("👆 点击 '校内账号登录' (CSS)...")
                elem.click()
                return True
        except:
            pass

    except:
        pass
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
    add_log(f"🚀 [{username}] 启动智能登录 (60s超时)...")
    
    if not driver:
        driver = init_browser()
        if not driver: return "error", "浏览器启动失败"
    
        if not driver: return "error", "浏览器启动失败"
    
    add_log(f"🌐 [{username}] 浏览器就绪，正在打开登录页...")
    # 确保打开页面
    if "venue" not in driver.current_url and "sso" not in driver.current_url:
        driver.get("https://venue.spe.scut.edu.cn/vb-user/login")
    add_log(f"📄 当前页面标题: {driver.title}")

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
            try:
                cookies = {c['name']: c['value'] for c in driver.get_cookies()}
                # add_log(f"🎉 [{username}] 成功获取 Token，🍪 捕获 Cookies ({len(cookies)})")
            except:
                pass
            
            close_driver(driver)
            
            # --- 保存会话信息 (新增) ---
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "token": token,
                    "cookies": cookies,
                    "password": password, # 保存密码用于救援
                    "last_updated": time.time()
                }
            
            return "success", {"token": token, "cookies": cookies}

        # 2. 检测 2FA 界面 (#PM1 是特定的验证码框ID)
        try:
            if len(driver.find_elements(By.ID, "PM1")) > 0:
                add_log(f"⚠️ [{username}] 检测到双重验证 (2FA) 界面，暂停等待输入...")
                # 返回 Driver 实例以供后续 2FA 使用
                with DRIVER_MAP_LOCK: PENDING_DRIVERS[username] = driver
                return "need_2fa", "等待验证码"
        except:
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
                add_log("🖱️ 凭证已填充，尝试点击登录按钮...")
                if click_login_btn(driver):
                    add_log("⏳ 点击成功，等待页面跳转...")
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

def fetch_venue_data(token, date_str, cookies=None, username=None):
    """
    使用 chaxun.txt 的逻辑进行数据查询，支持 Cookie 和 自动救援
    """
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    ts = int(dt.replace(hour=0,minute=0,second=0).timestamp() * 1000)
    url = "https://venue.spe.scut.edu.cn/api/pc/venue/pc/booking"
    
    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
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
        # 1. 尝试第一次请求
        print(f"DEBUG: fetch_venue_data calling requests.post... token={token[:10]}...", flush=True)
        resp = requests.post(url, headers=headers, json=payload, cookies=cookies, timeout=8)
        print(f"DEBUG: fetch_venue_data response: {resp.status_code}", flush=True)
        
        # 2. 核心救援逻辑：检测是否返回了 HTML (登录页)
        # 关键：检查 Content-Type 确保真的是 HTML 页面，避免误判
        content_type = resp.headers.get('Content-Type', '').lower()
        is_html_page = 'text/html' in content_type
        
        # 调试：记录响应类型（临时）
        if username and is_html_page:
            add_log(f"🔍 [DEBUG] 响应 Content-Type: {content_type}, 状态码: {resp.status_code}")
        
        if resp.status_code == 200 and is_html_page:
            if username:
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
        
        # 3. 解析正常响应 (首次成功 或 重试成功)
        if resp.status_code == 200:
            try:
                res_json = resp.json()
                print(f"DEBUG: fetch_venue_data json: {str(res_json)[:100]}", flush=True)
                if (res_json.get("code") == 1 or res_json.get("code") == 200) and "data" in res_json:
                    return res_json["data"].get("venueSessionResponses", [])
            except:
                pass # JSON 解析失败，或者仍然是 HTML
                
    except Exception as e:
        add_log(f"❌ 数据查询异常: {e}")
    return None
def check_token_validity(token, cookies=None, username=None):
    """检查 Token/Cookie 是否仍可用于获取订场数据（通过 booking 接口探测）。"""
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        sessions = fetch_venue_data(token, today, cookies, username=username)
        # fetch_venue_data 失败时返回 None
        print(f"DEBUG: check_token_validity result: {sessions is not None}", flush=True)
        return sessions is not None
    except:
        print("DEBUG: check_token_validity exception", flush=True)
        return False

def get_booking_params(date_str):
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    tz_utc8 = datetime.timezone(datetime.timedelta(hours=8))
    dt = dt.replace(tzinfo=tz_utc8)
    timestamp = int(dt.timestamp() * 1000)
    weekday = dt.isoweekday()
    return timestamp, weekday

def send_booking_request(token, user_id, date_str, start_time, end_time, venue_id, price=40, stadium_id=1, cookies=None):
    belong_date, week = get_booking_params(date_str)
    url = "https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/apply"

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
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
        # 关键修复：带上 Cookies
        resp = requests.post(url, headers=headers, json=payload, cookies=cookies, timeout=5)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 200 or "成功" in str(res_json):
                return True, "预定成功"
            return False, res_json.get("msg", str(res_json))
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

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
