from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import time
import threading
import requests
import datetime
import re
import base64
import uuid
import sys
import os
import subprocess
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv

# 加载环境变量 (默认读取 .env)
load_dotenv()


# --- 1. 日志净化 ---
# 禁用 Flask 默认的请求日志，只保留错误
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
# 允许跨域
CORS(app, resources={r"/*": {"origins": "*"}})

# ================= 全局变量与任务管理 =================
DRIVER_PATH = None

# --- 并发与资源控制 ---
# 限制同时运行的浏览器数量 (防止内存/CPU爆炸)
BROWSER_LIMIT = 2
BROWSER_SEMAPHORE = threading.Semaphore(BROWSER_LIMIT)

# 活跃浏览器进程 ID 集合 (用于精确清理)
ACTIVE_DRIVER_PIDS = set()
PID_LOCK = threading.Lock()

def cleanup_at_exit():
    """ 退出时清理所有残留的浏览器进程 """
    with PID_LOCK:
        if not ACTIVE_DRIVER_PIDS:
            return
        print(f"🧹 正在清理 {len(ACTIVE_DRIVER_PIDS)} 个残留浏览器进程...")
        for pid in list(ACTIVE_DRIVER_PIDS):
            try:
                if sys.platform.startswith('win'):
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.kill(pid, 9)
            except:
                pass
        ACTIVE_DRIVER_PIDS.clear()

atexit.register(cleanup_at_exit)

# --- 多用户隔离设计 ---
# USER_SESSIONS 存储结构: 
# { 
#   "username1": { "password": "...", "email": "...", "token": "...", "cookies": {...}, "last_updated": ts },
#   "username2": ...
# }
USER_SESSIONS = {}
SESSION_LOCK = threading.Lock()


# --- 订单缓存（避免“我的订单”切换标签反复打平台接口）---
# 结构:
# ORDER_CACHE[cache_key] = {
#   "updated_at": ts,
#   "by_status": {1: [records...], 2: [...], 3: [...], 4: [...]}
# }
ORDER_CACHE = {}
ORDER_CACHE_LOCK = threading.Lock()
# “我的订单”页面打开后，短时间内切换标签直接读缓存；超过 TTL 再触发一次全量刷新
ORDER_CACHE_TTL_SECONDS = 60
# 每个状态最多抓取的页数与每页条数（平台分页）
ORDER_MAX_PAGES = 5
ORDER_PAGE_SIZE = 20

# 暂存正在进行 2FA 登录的 Driver，Key=username
PENDING_DRIVERS = {} 
DRIVER_LOCK = threading.Lock()

# 任务管理器
TASK_MANAGER = {}
TASK_LOCK = threading.Lock()
ALLOWLIST_LOCK = threading.Lock()

# 全局日志缓冲区
GLOBAL_LOGS = []
MAX_LOG_LENGTH = 200
LOG_LOCK = threading.Lock()

def add_log(msg):
    """ 添加日志到全局缓冲区，并打印到控制台 """
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    print(full_msg)
    sys.stdout.flush()

    with LOG_LOCK:
        GLOBAL_LOGS.append(full_msg)
        if len(GLOBAL_LOGS) > MAX_LOG_LENGTH:
            GLOBAL_LOGS.pop(0)


# ================= 邮件服务 =================

def send_email_notification(receiver, account_name, order_info):
    """ 发送邮件通知 """
    if not receiver:
        return

    smtp_server = "smtp.qq.com"
    smtp_port = 465
    sender = "1696725502@qq.com"
    password = "voqujocowzfrccdh"  # 授权码

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

    smtp_server = "smtp.qq.com"
    smtp_port = 465
    sender = "1696725502@qq.com"
    password = "voqujocowzfrccdh"  # 授权码

    subject = f'⚠️ 锁场失败警告：账号 {account_name} 场地已丢失'

    content = f"""账号 [{account_name}] 锁场模式异常退出！

目标场地：{venue_name}
失败原因：{fail_reason}

系统尝试在10秒内连续续订失败，场地可能已被他人抢走或系统限制。
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
# ================= 浏览器与登录核心 =================

def kill_zombie_processes():
    """ 
    尝试清理残留的 chrome 进程
    现在改用精确的 PID 清理，此函数主要作为手动触发的强力GC 
    """
    cleanup_at_exit()

def init_browser():
    """ 
    工厂模式：每次调用返回全新的 driver 实例 
    不再依赖全局 driver_instance 
    """
    global DRIVER_PATH
    
    # 1. 驱动检查 - 优先使用系统常见路径
    if not DRIVER_PATH:
        possible_paths = [
            "/usr/bin/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
            "/usr/local/bin/chromedriver",
            "/snap/bin/chromium.chromedriver"
        ]
        for p in possible_paths:
            if os.path.exists(p):
                DRIVER_PATH = p
#                add_log(f"✅ 使用系统驱动: {p}")
                break
        
        # 找不到则尝试自动下载
        if not DRIVER_PATH:
            try: DRIVER_PATH = ChromeDriverManager().install()
            except: pass

    if not DRIVER_PATH:
        add_log("❌ 致命错误: 未找到 ChromeDriver")
        return None

    # 2. 启动逻辑 (使用 port=0 解决端口冲突)
    options = webdriver.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    
    # 允许通过环境变量控制是否开启 headless (方便调试)
    if os.environ.get("HEADLESS", "true").lower() != "false":
        options.add_argument("--headless=new")
        
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=0") # 关键：随机端口
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    # 3. 获取并发许可
    acquired = BROWSER_SEMAPHORE.acquire(blocking=True, timeout=30)
    if not acquired:
        add_log("⏳ 服务器繁忙: 浏览器实例已达上限，请稍后...")
        return None

    try:
        for attempt in range(2):
            try:
                # 每次实例化一个新的 Service，确保端口独立
                service = Service(executable_path=DRIVER_PATH, port=0)
                driver = webdriver.Chrome(service=service, options=options)
                driver.set_page_load_timeout(30)
                
                # 标记该 driver 已持有信号量
                driver._semaphore_acquired = True
                
                # 记录 PID
                try:
                    pid = driver.service.process.pid
                    with PID_LOCK:
                        ACTIVE_DRIVER_PIDS.add(pid)
                    driver._pid = pid
                except:
                    pass
                
                return driver
            except Exception as e:
                add_log(f"⚠️ 启动尝试 {attempt+1} 失败: {e}")
                if attempt == 1:
                    # 最后一次尝试失败，需要释放信号量
                    BROWSER_SEMAPHORE.release()
                    return None
    except:
        # 异常兜底释放
        BROWSER_SEMAPHORE.release()
        return None


def close_driver(driver):
    if driver:
        # 1. 释放信号量
        if getattr(driver, '_semaphore_acquired', False):
            BROWSER_SEMAPHORE.release()
            driver._semaphore_acquired = False
            
        # 2. 移除 PID 记录
        pid = getattr(driver, '_pid', None)
        if pid:
            with PID_LOCK:
                ACTIVE_DRIVER_PIDS.discard(pid)

        # 3. 关闭驱动
        try:
            driver.quit()
        except:
            pass



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


def execute_login_logic(username, password):
    """
    执行登录流程。
    返回: (status, result_dict)
    - status: "success", result={"token": "...", "cookies": {...}}
    - status: "need_2fa", result=driver
    - status: "error", result=msg
    """
    add_log(f"🚀 [{username}] 启动智能登录 (60s超时)...")
    
    driver = init_browser()
    if not driver: return "error", "Browser failed"

    # 确保打开页面
    if "venue" not in driver.current_url and "sso" not in driver.current_url:
        driver.get("https://venue.spe.scut.edu.cn/vb-user/login")

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
        token = sniff_token(driver)
        if token:
#            add_log(f"🎉 [{username}] 成功获取 Token")
            # --- 关键修改：获取 Cookies ---
            # 稍作等待确保 Cookie 写入
            time.sleep(0.5) 
            cookies = {}
            try:
                cookies = {c['name']: c['value'] for c in driver.get_cookies()}
                add_log(f"🎉 [{username}] 成功获取 Token，🍪 捕获 Cookies ({len(cookies)})")
            except:
                pass
            
            close_driver(driver)
            return "success", {"token": token, "cookies": cookies}

        # 2. 检测 2FA 界面 (#PM1 是特定的验证码框ID)
        try:
            if len(driver.find_elements(By.ID, "PM1")) > 0:
                add_log(f"⚠️ [{username}] 检测到双重验证 (2FA) 界面，暂停等待输入...")
                # 返回 Driver 实例以供后续 2FA 使用
                return "need_2fa", driver
        except:
            pass

        # 3. 页面动作 (每隔2秒执行一次，避免频繁操作)
        if time.time() - last_action_time < 2:
            continue

        last_action_time = time.time()

        # A. 检测 "校内账号登录" 并点击
        if check_and_click_campus_login(driver):
            add_log("🔄 正在跳转...")
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
#                add_log("🖱️ 凭证就绪，尝试点击登录...")
                if click_login_btn(driver):
#                    add_log("⏳ 等待跳转...")
                    time.sleep(2)
            continue

    # 超时
    close_driver(driver)
    return "error", "Login Timeout (60s)"


# ================= 数据与核心逻辑 =================

def extract_user_info(token):
    """
    从 JWT payload 中提取 userId 与可作为会话键的账号（优先学号 sno/account）。
    注意：payload 根字段 account 可能是 $sign:...（脱敏/加密），不能作为会话键。
    """
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))

        ui = data.get("userInfo") or {}
        account = ui.get("sno") or ui.get("account")

        if not account:
            root_acc = data.get('account') or data.get('username') or data.get('nickname')
            if isinstance(root_acc, str) and root_acc.startswith("$sign:"):
                root_acc = None
            account = root_acc

        user_id = data.get('userId') or ui.get('userId')
        return {
            "userId": user_id,
            "account": account or (str(user_id) if user_id is not None else None)
        }
    except:
        return None


def get_booking_params(date_str):
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    tz_utc8 = datetime.timezone(datetime.timedelta(hours=8))
    dt = dt.replace(tzinfo=tz_utc8)
    timestamp = int(dt.timestamp() * 1000)
    weekday = dt.isoweekday()
    return timestamp, weekday


def fetch_venue_data_internal(token, date_str, cookies=None, username=None):
    """
    使用 chaxun.txt 的逻辑进行数据查询，支持 Cookie 和 自动救援
    """
    ts, _ = get_booking_params(date_str)
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
        resp = requests.post(url, headers=headers, json=payload, cookies=cookies, timeout=8)
        
        # 2. 核心救援逻辑：检测是否返回了 HTML (登录页)，如果是则代表 Session 失效
        if resp.status_code == 200 and ("<html" in resp.text.lower() or "doctype html" in resp.text.lower()):
            if username:
                add_log(f"⚠️ [{username}] Token/Cookie失效，触发自动救援...")
                
                # 尝试找回密码
                pwd = None
                with SESSION_LOCK:
                    if username in USER_SESSIONS:
                        pwd = USER_SESSIONS[username].get('password')
                
                if pwd:
                    add_log(f"🔄 正在后台重新登录 {username}...")
                    # 重新执行登录
                    status, res = execute_login_logic(username, pwd)
                    
                    if status == "success":
                        new_token = res['token']
                        new_cookies = res['cookies']
                        
                        # 更新全局缓存
                        with SESSION_LOCK:
                            if username in USER_SESSIONS:
                                USER_SESSIONS[username]['token'] = new_token
                                USER_SESSIONS[username]['cookies'] = new_cookies
                                USER_SESSIONS[username]['last_updated'] = time.time()
                        
                        add_log("✅ 救援成功！使用新凭证重试请求...")
                        # 使用新凭证重试
                        headers["authorization"] = f"Bearer {new_token}"
                        resp = requests.post(url, headers=headers, json=payload, cookies=new_cookies, timeout=8)
                        
                        # 立即解析结果
                        if resp.status_code == 200:
                            res_json = resp.json()
                            if (res_json.get("code") == 1 or res_json.get("code") == 200) and "data" in res_json:
                                return res_json["data"].get("venueSessionResponses", [])
                    else:
                        add_log(f"❌ 救援失败: {res}")
                else:
                    add_log("❌ 无法救援: 缺少保存的密码")
        
        # 3. 解析正常响应 (首次成功 或 重试成功)
        if resp.status_code == 200:
            try:
                res_json = resp.json()
                if (res_json.get("code") == 1 or res_json.get("code") == 200) and "data" in res_json:
                    return res_json["data"].get("venueSessionResponses", [])
            except:
                pass # JSON 解析失败，或者仍然是 HTML
                
    except Exception as e:
        add_log(f"❌ 数据查询异常: {e}")
    return None

def ms_to_dt(ms):
    """毫秒时间戳转为 'YYYY-MM-DD HH:MM:SS'，为空返回空字符串。"""
    try:
        if not ms:
            return ""
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return ""

def _extract_orders_from_payload(payload):
    """兼容不同分页结构：常见是 payload['data']['records'] 或 payload['data']['list']。"""
    data = payload.get("data")
    if isinstance(data, dict):
        for k in ("records", "list", "rows", "items"):
            if isinstance(data.get(k), list):
                return data[k]
    if isinstance(data, list):
        return data
    return []

def _normalize_order_records(payload):
    """
    将平台返回的订单分页数据扁平化为前端可直接渲染的 records 列表：
    每条记录代表一个具体 rental（场地+日期+时段）。
    """
    raw_orders = _extract_orders_from_payload(payload)
    records = []
    for o in raw_orders:
        # 只保留羽毛球项目（与用户提供的抓取脚本一致）
        if o.get("projectName") and o.get("projectName") != "羽毛球":
            continue

        rentals = o.get("rentals") or []
        for r in rentals:
            belong_date = ms_to_dt(r.get("belongDate"))[:10]  # 只取日期
            records.append({
                # 前端渲染所需字段（index.js 中使用 fieldName/belongDate/startTime/endTime/price/statusDesc）
                "fieldName": o.get("projectName") or "羽毛球",
                "belongDate": belong_date,
                "startTime": r.get("startTime") or r.get("start") or "",
                "endTime": r.get("endTime") or r.get("end") or "",
                "venueName": r.get("venueName") or r.get("venue") or "",
                "price": float(o.get("receivable") or o.get("receipts") or o.get("amount") or 0),

                # 额外信息：便于对账/排障
                "orderNo": o.get("orderNo"),
                "statusDesc": o.get("statusDesc") or o.get("statusName") or o.get("status") or "",
                "receivable": o.get("receivable"),
                "paidAt": ms_to_dt(o.get("paidAt")),
                "paidAtMs": int(o.get("paidAt") or 0),
                "createdAt": ms_to_dt(o.get("createdAt")),
                "createdAtMs": int(o.get("createdAt") or 0),
            })

    # 保留分页元信息（如果存在）
    data = payload.get("data")
    if isinstance(data, dict):
        return {
            "records": records,
            "page": data.get("page"),
            "pageSize": data.get("pageSize") or data.get("size"),
            "total": data.get("total"),
        }
    records.sort(key=lambda x: int(x.get("createdAtMs") or 0), reverse=True)

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

def check_token_validity(token, cookies=None, username=None):
    """检查 Token/Cookie 是否仍可用于获取订场数据（通过 booking 接口探测）。"""
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        sessions = fetch_venue_data_internal(token, today, cookies, username=username)
        # fetch_venue_data_internal 失败时返回 None
        return sessions is not None
    except:
        return False

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
    status, res = execute_login_logic(username, pwd)
    
    if status == "success":
        new_token = res['token']
        new_cookies = res['cookies']
        
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                USER_SESSIONS[username]['token'] = new_token
                USER_SESSIONS[username]['cookies'] = new_cookies
                USER_SESSIONS[username]['last_updated'] = time.time()
        
        add_log(f"✅ [{username}] 救援成功！")
        return True
    else:
        add_log(f"❌ [{username}] 救援失败: {res}")
        return False


# --- Workers ---

def monitor_worker(task_id, stop_event, token, user_id_obj, date, start_time, end_time, is_lock_mode, initial_price=40,
                   email_receiver=None, account_name=None, target_venue_id=None, target_venue_name=None):
    mode_str = "无限锁场" if is_lock_mode else "狙击抢票"
    add_log(f"👀 [Task {task_id}] 开始监控: {date} {start_time} ({mode_str})")

    user_id = user_id_obj.get('userId')
    if not account_name:
        account_name = user_id_obj.get('account')

    # 计算停止时间
    try:
        target_dt_str = f"{date} {start_time}"
        target_dt = datetime.datetime.strptime(target_dt_str, "%Y-%m-%d %H:%M")
    except:
        target_dt = None

    actual_price = initial_price
    email_sent_once = False
    
    current_token = token
    # 初始化 cookies
    current_cookies = {}
    with SESSION_LOCK:
        if account_name in USER_SESSIONS:
            current_cookies = USER_SESSIONS[account_name].get('cookies', {})

    while not stop_event.is_set():
        # 0. 自动救援同步
        with SESSION_LOCK:
            if account_name in USER_SESSIONS:
                cached = USER_SESSIONS[account_name]
                # 如果缓存的 token 变了，说明被自动救援更新了，我们跟进
                if cached.get('token') and cached.get('token') != current_token:
                    current_token = cached['token']
                    current_cookies = cached.get('cookies', {})
                    add_log(f"🔄 [Task {task_id}] 同步到新凭证")

        # 1. 自动停止检查
        if target_dt:
            now = datetime.datetime.now()
            if now > target_dt + datetime.timedelta(minutes=1):
                add_log(f"🛑 [Task {task_id}] 已到达目标时间 {start_time}，任务自动结束。")
                with TASK_LOCK:
                    if task_id in TASK_MANAGER:
                        TASK_MANAGER[task_id]['status'] = "已完成"
                stop_event.set()
                break

        # add_log(f"[Monitor {task_id}] 扫描中 {date} {start_time}...")

        # 传入 username 以支持 worker 内部的 fetch 触发自动救援
        sessions = fetch_venue_data_internal(current_token, date, current_cookies, username=account_name)

        # 过滤目标：如果前端指定了 venueId，则只锁定/抢该场地；否则沿用旧逻辑（同时间段取第一个空闲）
        target_session = None

        # 统一为字符串比较，避免 int/str 不一致
        target_vid = str(target_venue_id) if target_venue_id is not None else None

        for s in sessions or []:
            try:
                if s.get('startTime') != start_time:
                    continue
                # 额外匹配 end_time，避免同 startTime 不同长度的场次误命中
                if end_time and s.get('endTime') and s.get('endTime') != end_time:
                    continue
                if int(s.get('availNum', 0)) != 1:
                    continue

                if target_vid:
                    if str(s.get('venueId')) != target_vid:
                        continue

                # 命中
                target_session = s
                if 'price' in s:
                    actual_price = s['price']
                break
            except Exception:
                continue

        if target_session:
            add_log(f"🎉 [Task {task_id}] 发现空闲: {target_session['venueName']}")
            
            ok, msg = send_booking_request(current_token, user_id, date, start_time, end_time, target_session['venueId'],
                                           actual_price, cookies=current_cookies)

            if ok:
                if not email_sent_once:
                    order_info = f"日期: {date}\n时间: {start_time}-{end_time}\n场地: {target_session['venueName']} (ID: {target_session['venueId']})"
                    send_email_notification(email_receiver, account_name, order_info)
                    email_sent_once = True 

                if is_lock_mode:
                    with TASK_LOCK:
                        if task_id in TASK_MANAGER: 
                            TASK_MANAGER[task_id]['status'] = f"已锁场: {target_session['venueName']}"

                    add_log(f"🔒 锁定成功，进入保活循环 (每10分钟高频续订)...")

                    while not stop_event.is_set():
                        # 1. 检查是否到达整体任务结束时间
                        if target_dt and datetime.datetime.now() > target_dt + datetime.timedelta(minutes=1):
                            add_log(f"🛑 [Lock {task_id}] 到达任务截止时间，停止锁场。")
                            stop_event.set()
                            break

                        # 2. 等待阶段 & 前置检查
                        # 策略：总周期600s (10分钟)。
                        # 在 T+530s (8分50秒) 进行 Token 检查
                        # 在 T+540s (9分00秒) 开始持续 70s 的爆发续订 (覆盖到 T+610s)
                        
                        WAIT_PHASE_1 = 530
                        for remaining in range(WAIT_PHASE_1, 0, -1):
                            if stop_event.is_set(): return
                            time.sleep(1)

                        # --- 前置 Token 检查 (T+530s) ---
                        if not stop_event.is_set():
                            add_log(f"🔎 [Lock {task_id}] 续订前置检查: 校验 Token 有效性...")
                            token_valid = False
                            try:
                                # 发送轻量级请求 (查询当日场地)，failure means token/cookie invalid
                                check_res = fetch_venue_data_internal(current_token, date, current_cookies, username=None)
                                if check_res is not None:
                                    token_valid = True
                            except: pass

                            if token_valid:
                                add_log(f"✅ [Lock {task_id}] Token 有效")
                            else:
                                add_log(f"⚠️ [Lock {task_id}] Token 失效，触发自动救援...")
                                if try_rescue_token(account_name, reason="pre_check_failed"):
                                    add_log(f"✅ [Lock {task_id}] 救援成功，准备续订")
                                else:
                                    add_log(f"❌ [Lock {task_id}] 救援失败，将使用旧凭证尝试")

                        # 等待到 T+540s (再次等待 10s)
                        for _ in range(10):
                            if stop_event.is_set(): return
                            time.sleep(1)

                        # 3. 爆发续订阶段：持续 70 秒 (覆盖原定第10分钟的掉单时刻)
                        add_log(f"⚡ [Lock {task_id}] 唤醒！开始 70秒 深度保活续订...")
                        
                        burst_start_time = time.time()
                        burst_duration = 70 
                        round_success = False 

                        while time.time() - burst_start_time < burst_duration:
                            if stop_event.is_set(): return

                            # --- Token 同步逻辑 ---
                            with SESSION_LOCK:
                                if account_name in USER_SESSIONS:
                                    cached = USER_SESSIONS[account_name]
                                    if cached.get('token') != current_token:
                                        current_token = cached['token']
                                        current_cookies = cached.get('cookies', {})
                            # --------------------

                            # 发送请求
                            ok_renew, msg_renew = send_booking_request(
                                current_token, user_id, date, start_time, end_time,
                                target_session['venueId'], actual_price, cookies=current_cookies
                            )

                            if ok_renew:
                                add_log(f"✅ [Lock {task_id}] 续订成功！")
                                round_success = True
                                break 
                            
                            time.sleep(0.5) # 稍微放慢间隔，避免请求过于密集被封

                        # 4. 结果判定
                        if not round_success:
                            add_log(f"❌ [Lock {task_id}] 本轮续订全部失败，场地可能已丢失。")
                            # 失败也不退出，继续尝试下一轮？不行，场地丢了就是丢了，锁场无意义
                            # 但为了保险，可以发邮件通知
                            with TASK_LOCK:
                                if task_id in TASK_MANAGER: 
                                    TASK_MANAGER[task_id]['status'] = f"锁场失败: {target_session['venueName']}"
                            send_lock_failed_email(email_receiver, account_name, target_session['venueName'], fail_reason="Renew Failed")
                            stop_event.set()
                            break
                        else:
                            add_log(f"⏸️ [Lock {task_id}] 本轮保活完成，等待下一个周期...")
                

                else:
                    # 普通抢票模式逻辑
                    with TASK_LOCK:
                        if task_id in TASK_MANAGER: 
                            TASK_MANAGER[task_id]['status'] = f"抢票成功: {target_session['venueName']}"
                    add_log(f"✅ 抢票成功，任务结束。")
                    stop_event.set()


# ================= API Endpoints =================

def check_whitelist(username):
    """
    检查用户名是否在白名单中（allowed_users.txt 每行一个账号）。
    - 支持空行与以 # 开头的注释行
    - 如果文件不存在：自动创建一个模板文件（但仍然拒绝登录，更安全）
    - 可通过环境变量 SCUT_ALLOWLIST_FILE 指定白名单路径
    """
    allowlist_path = os.environ.get("SCUT_ALLOWLIST_FILE", "allowed_users.txt")
    try:
        if not os.path.exists(allowlist_path):
            # 自动创建模板，避免“文件不存在导致无法配置”的尴尬
            with open(allowlist_path, "w", encoding="utf-8") as f:
                f.write("# 允许登录的用户名（每行一个学号/账号）")
                f.write("# 例如：202320100034")
            return False

        allowed = set()
        with ALLOWLIST_LOCK:
            with open(allowlist_path, "r", encoding="utf-8") as f:
                for line in f:
                    # 去除行内注释和空白
                    s = line.split('#')[0].strip()
                    if not s:
                        continue
                    allowed.add(s)

        return str(username).strip() in allowed
    except Exception as e:
        add_log(f"⚠️ 白名单校验出错: {e}")
        return False  # 出错默认拒绝，确保安全


@app.route('/api/login', methods=['POST'])
def handle_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')

    # --- 0. 白名单校验 ---
    if not check_whitelist(username):
        add_log(f"⛔ [{username}] 未授权用户尝试登录，已拦截。")
#        return jsonify({"status": "denied", "msg": "需要获取权限请联系1696725502@qq.com这个邮箱，并备注相关理由。"}), 403
        return jsonify({"status": "forbidden", "msg": "Access Denied"})
    # 1. 检查缓存
    with SESSION_LOCK:
        if username in USER_SESSIONS:
            cached = USER_SESSIONS[username]
            # 只有当密码匹配时才复用
            if cached.get('password') == password:
                token = cached.get('token')
                cookies = cached.get('cookies')
                # 简单验证 Token 有效性
                if check_token_validity(token, cookies, username=username):
                    add_log(f"⚡ [{username}] 使用缓存 Token 秒登成功")
                    return jsonify({"status": "success", "token": token})
    
    # 2. 如果缓存无或无效，执行 Selenium 登录
    with DRIVER_LOCK:
        if username in PENDING_DRIVERS:
             close_driver(PENDING_DRIVERS[username])
             del PENDING_DRIVERS[username]

    try:
        status, result = execute_login_logic(username, password)
        
        if status == "success":
            # result 包含 token 和 cookies
            token = result['token']
            cookies = result['cookies']
            
            # 登录成功，更新缓存
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "password": password,
                    "email": email,
                    "token": token,
                    "cookies": cookies,
                    "last_updated": time.time()
                }
            return jsonify({"status": "success", "token": token})
        
        elif status == "need_2fa":
            # 暂存 driver 以便后续验证
            with DRIVER_LOCK:
                PENDING_DRIVERS[username] = result

            # 暂存凭证（用于 2FA 完成后写入 Session，及后续自动救援）
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "password": password,
                    "email": email,
                    "token": USER_SESSIONS.get(username, {}).get("token"),
                    "cookies": USER_SESSIONS.get(username, {}).get("cookies"),
                    "last_updated": time.time()
                }

            return jsonify({"status": "need_2fa", "msg": "请输入验证码"})
        
        else:
            return jsonify({"status": "error", "msg": result})

    except Exception as e:
        add_log(f"❌ Login Error: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/submit_2fa', methods=['POST'])
def handle_2fa():
    # 必须传 username 以识别对应的 driver
    data = request.json
    code = data.get('code')
    username = data.get('username')
    
    if not username: return jsonify({"status": "error", "msg": "Missing username"})

    driver = None
    with DRIVER_LOCK:
        driver = PENDING_DRIVERS.get(username)
    
    if not driver: return jsonify({"status": "error", "msg": "Session expired or browser closed"})

    add_log(f"📨 [{username}] 提交验证码: {code}")

    try:
        # 使用用户提供的特定 ID: #PM1
        input_box = WebDriverWait(driver, 5).until(
            EC.visibility_of_element_located((By.ID, "PM1"))
        )
        input_box.clear()
        input_box.send_keys(code)

        # 尝试点击登录
        # 优先尝试点击 <span> 父容器，因为用户结构显示 input 是里面的一个元素，点击 span 可能更稳
        clicked = False
        try:
            # 1. 尝试 input
            btn = driver.find_element(By.CSS_SELECTOR, "#index_login_btn > input")
            btn.click()
            clicked = True
        except:
            try:
                # 2. 尝试 span 容器
                btn = driver.find_element(By.ID, "index_login_btn")
                btn.click()
                clicked = True
            except:
                # 3. JS 强制点击
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, ".login_box_landing_btn")
                    driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                except: pass
        
        if not clicked:
             add_log(f"⚠️ [{username}] 无法找到登录提交按钮")

        # 等待更长的时间 (15s)，且 sniff_token 现在会真正轮询
        token = sniff_token(driver, timeout=15)
        
        if token:
            # 提取 Cookies
            cookies = {}
            try:
                cookies = {c['name']: c['value'] for c in driver.get_cookies()}
            except: pass
            
            close_driver(driver)
            # 移除 pending
            with DRIVER_LOCK:
                if username in PENDING_DRIVERS: del PENDING_DRIVERS[username]
            
            # 更新 Session
            with SESSION_LOCK:
                USER_SESSIONS[username] = {
                    "token": token,
                    "cookies": cookies,
                    "last_updated": time.time(),
                    # 保存本次凭证（用于后续自动救援）
                    # 注意：这里 password/email 需要从之前的 session 继承或保留，防止覆盖为空
                    "password": USER_SESSIONS.get(username, {}).get("password"),
                    "email": USER_SESSIONS.get(username, {}).get("email")
                }
            
            add_log(f"🎉 [{username}] 验证成功，已登录")
            return jsonify({"status": "success", "token": token})
        else:
            add_log(f"❌ [{username}] 2FA 验证后未检测到 Token (超时)")
            return jsonify({"status": "error", "msg": "验证超时或失败"})

    except Exception as e:
        add_log(f"❌ 2FA Error: {e}")
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/venues', methods=['GET'])
def get_venues_proxy():
    token = request.args.get('token')
    if not token: return jsonify({"error": "No token"}), 400
    
    # 尝试根据 token 找到对应的 cookies
    user_info = extract_user_info(token)
    username = request.args.get('username') or (user_info.get('account') if user_info else None)
    
    cookies = {}
    if username:
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                cookies = USER_SESSIONS[username].get('cookies', {})

    dates = [(datetime.datetime.now() + datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(8)]
    result = {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        # 传递 username 以启用自动救援
        futures = {ex.submit(fetch_venue_data_internal, token, d, cookies, username): d for d in dates}
        for f in as_completed(futures):
            d = futures[f]
            raw_list = f.result()

            venue_map = {}
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

    return jsonify(result)


@app.route('/api/book/direct', methods=['POST'])
def book_direct():
    data = request.json
    token = data.get('token')
    email = data.get('email')
    username = data.get('username') 

    user_info = extract_user_info(token)
    if not user_info: return jsonify({"status": "error", "msg": "Invalid Token"}), 401

    account_name = username if username else user_info['account']
    
    # 获取 cookies
    cookies = {}
    with SESSION_LOCK:
        if account_name in USER_SESSIONS:
            if email: USER_SESSIONS[account_name]['email'] = email
            cookies = USER_SESSIONS[account_name].get('cookies', {})

    add_log(f"⚡ [Direct] 尝试预定 {data['startTime']} 的场地...")
    ok, msg = send_booking_request(
        token, user_info['userId'],
        data['date'], data['startTime'], data['endTime'],
        data['venueId'], data.get('price', 40), data.get('stadiumId', 1),
        cookies=cookies
    )
    if ok:
        add_log("✅ 预定成功")
        order_details = f"日期: {data['date']}\n时间: {data['startTime']}-{data['endTime']}\n场馆ID: {data['venueId']}"
        send_email_notification(email, account_name, order_details)
    else:
        add_log(f"❌ 预定失败: {msg}")

    return jsonify({"status": "success" if ok else "error", "msg": msg})


@app.route('/api/task/monitor', methods=['POST'])
def start_monitor():
    data = request.json
    token = data.get('token')
    email = data.get('email')
    username = data.get('username')

    user_info = extract_user_info(token)
    if not user_info: return jsonify({"status": "error", "msg": "Invalid Token"}), 401

    account_name = username if username else user_info['account']
    
    if username and email:
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                 USER_SESSIONS[username]['email'] = email

    task_id = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    is_lock = data.get('lockMode', False)

    with TASK_LOCK:
        type_str = "lock" if is_lock else "snipe"
        info_str = f"[{account_name}] {data['date']} {data['startTime']}"
        TASK_MANAGER[task_id] = {"type": type_str, "status": "准备中", "stop_event": stop_event, "info": info_str}

    t = threading.Thread(target=monitor_worker, args=(
        task_id, stop_event, token, user_info,
        data['date'], data['startTime'], data['endTime'],
        is_lock, data.get('price', 40), email, account_name,
        data.get('venueId'), data.get('venueName') or data.get('name')
    ))
    t.daemon = True
    t.start()

    with TASK_LOCK:
        TASK_MANAGER[task_id]['status'] = "监控中"

    return jsonify({"status": "success", "taskId": task_id, "msg": "监控任务已启动"})



def _fetch_orders_pages(token, status_value, cookies=None, username=None, max_pages=ORDER_MAX_PAGES, page_size=ORDER_PAGE_SIZE):
    """抓取某个 status 的多页订单并扁平化为 records 列表。"""
    all_records = []
    for page in range(1, max_pages + 1):
        payload = fetch_orders_internal(
            token,
            status_value,
            page=page,
            page_size=page_size,
            cookies=cookies,
            username=username
        )
        if not payload:
            break
        recs = payload.get("records") or []
        if not recs:
            break
        all_records.extend(recs)
        # 如果返回条数少于 page_size，通常已到末页
        if len(recs) < page_size:
            break

    # 再次排序（保险起见）
    all_records.sort(key=lambda x: int(x.get("createdAtMs") or 0), reverse=True)
    return all_records


def _paginate_records(records, page, page_size):
    """对缓存 records 做内存分页，返回 records 与总数。"""
    try:
        page = int(page or 1)
        page_size = int(page_size or 10)
    except:
        page, page_size = 1, 10
    page = max(page, 1)
    page_size = max(min(page_size, 200), 1)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "records": records[start:end],
        "total": len(records),
        "page": page,
        "pageSize": page_size
    }


@app.route('/api/orders', methods=['POST'])
def get_user_orders():
    data = request.json or {}
    token = data.get('token')
    # type: 'unpaid'(待支付), 'paid'(已支付), 'refund'(退款), 'closed'(已关闭)
    status_type = data.get('type', 'unpaid')
    username = data.get('username')

    # 1=待支付 2=已支付 3=退款 4=已关闭（与你新增的“获取账号订单.py”一致）
    status_map = {
        'unpaid': 1,
        'paid': 2,
        'refund': 3,
        'closed': 4
    }
    target_status = status_map.get(status_type, 1)

    if not token:
        return jsonify({"status": "error", "msg": "Missing token"})

    # cookies 优先从会话缓存取
    cookies = {}
    if username:
        with SESSION_LOCK:
            if username in USER_SESSIONS:
                cookies = USER_SESSIONS[username].get('cookies', {}) or {}

    # 缓存键：优先 username；否则退化为 token 末尾（避免不同用户串）
    cache_key = username or f"tk:{str(token)[-16:]}"
    now = time.time()

    # 是否强制刷新：前端可传 refreshAll=true（兼容未来扩展）
    force_refresh = bool(data.get("refreshAll") or data.get("forceRefresh") or data.get("prefetchAll"))

    # 如果缓存不存在或过期，则一次性抓取四种 status 并缓存
    with ORDER_CACHE_LOCK:
        cache = ORDER_CACHE.get(cache_key)

    need_refresh = force_refresh or (not cache) or (now - float(cache.get("updated_at", 0)) > ORDER_CACHE_TTL_SECONDS)

    if need_refresh:
        by_status = {}
        for st in (1, 2, 3, 4):
            recs = _fetch_orders_pages(token, st, cookies=cookies, username=username)
            by_status[st] = recs

        with ORDER_CACHE_LOCK:
            ORDER_CACHE[cache_key] = {"updated_at": now, "by_status": by_status}
        cache = ORDER_CACHE[cache_key]

    # 返回目标 status 的分页数据（内存分页、按 createdAtMs 降序）
    records = (cache.get("by_status") or {}).get(target_status, []) or []
    page = data.get("page", 1)
    page_size = data.get("pageSize", 10)
    result_data = _paginate_records(records, page, page_size)

    return jsonify({"status": "success", "data": result_data})


@app.route('/api/tasks', methods=['GET'])
def list_tasks():
    res = {}
    with TASK_LOCK:
        for tid, t in TASK_MANAGER.items():
            res[tid] = {"type": t["type"], "status": t["status"], "info": t["info"]}
    return jsonify(res)


@app.route('/api/task/stop', methods=['POST'])
def stop_task():
    tid = request.json.get('taskId')
    with TASK_LOCK:
        if tid in TASK_MANAGER:
            TASK_MANAGER[tid]['stop_event'].set()
            TASK_MANAGER[tid]['status'] = "Stopped"
            return jsonify({"status": "success", "msg": "Stopped"})
    return jsonify({"status": "error", "msg": "Not found"})


@app.route('/api/logs', methods=['GET'])
def get_logs_endpoint():
    with TASK_LOCK:
        return jsonify(GLOBAL_LOGS)

# ================= Admin 管理后台接口 =================

def _admin_key_ok(req):
    """ 校验管理密钥 """
    env_key = os.environ.get("SCUT_ADMIN_KEY", "")
    if not env_key: return True # 如果没设密码，默认允许（不建议）
    
    req_key = req.args.get("key") or req.headers.get("X-Admin-Key") or ""
    return req_key.strip() == env_key.strip()

@app.route("/admin", methods=["GET"])
def admin_page():
    # 只有 Admin 模式或密钥正确才允许访问
    if not _admin_key_ok(request):
        return "Access Denied: Invalid Key", 403

    allowlist_path = os.environ.get("SCUT_ALLOWLIST_FILE", "allowed_users.txt")
    content = ""
    try:
        if os.path.exists(allowlist_path):
            with ALLOWLIST_LOCK:
                with open(allowlist_path, "r", encoding="utf-8") as f:
                    content = f.read()
    except Exception as e:
        content = f"读取文件出错: {e}"

    # 简单的 HTML 界面
    html = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>SCUT 白名单管理</title>
        <style>body{{font-family: sans-serif; padding: 20px;}} textarea{{width:100%; height:300px; margin-top:10px;}}</style>
    </head>
    <body>
        <h2>🔐 SCUT 白名单管理后台</h2>
        <form onsubmit="addUser(); return false;">
            <input type="text" id="u" placeholder="输入学号/账号" required style="padding:5px;">
            <button type="submit" style="padding:5px 10px; cursor:pointer;">添加用户</button>
        </form>
        <p>当前白名单内容：</p>
        <textarea id="list" readonly>{content}</textarea>
        
        <script>
            async function addUser() {{
                const u = document.getElementById('u').value;
                const key = new URLSearchParams(window.location.search).get("key") || "";
                if(!u) return;
                
                try {{
                    const res = await fetch('/admin/add?key=' + key, {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{username: u}})
                    }});
                    const j = await res.json();
                    if(j.status === 'success') {{
                        alert('添加成功！');
                        location.reload();
                    }} else {{
                        alert('失败: ' + j.msg);
                    }}
                }} catch(e) {{ alert(e); }}
            }}
        </script>
    </body>
    </html>
    """
    return html

@app.route("/admin/add", methods=["POST"])
def admin_add_user():
    if not _admin_key_ok(request):
        return jsonify({"status": "denied", "msg": "Invalid Key"}), 403
        
    data = request.json or {}
    username = str(data.get("username", "")).strip()
    if not username:
        return jsonify({"status": "error", "msg": "用户名不能为空"}), 400
        
    allowlist_path = os.environ.get("SCUT_ALLOWLIST_FILE", "allowed_users.txt")
    
    try:
        # 输入清洗：去除首尾空格，禁止换行符
        username = username.replace("\n", "").replace("\r", "")
        if not username:
             return jsonify({"status": "error", "msg": "无效的用户名"}), 400

        with ALLOWLIST_LOCK:
            # 简单的去重检查
            current_users = set()
            if os.path.exists(allowlist_path):
                with open(allowlist_path, "r", encoding="utf-8") as f:
                    for line in f:
                        # 同样处理注释
                        s = line.split('#')[0].strip()
                        if s:
                            current_users.add(s)
            
            if username in current_users:
                 return jsonify({"status": "error", "msg": "用户已存在"}), 400

            with open(allowlist_path, "a", encoding="utf-8") as f:
                f.write(f"\n{username}")
            
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


if __name__ == '__main__':
    # === 关键修改：从环境变量读取配置 ===
    # 这样 systemd 里的 SCUT_PORT=5000 才能生效
    host = os.environ.get("SCUT_HOST", "0.0.0.0")
    port = int(os.environ.get("SCUT_PORT", "5001"))
    
    # 判断当前是 Admin 模式还是 Backend 模式
    is_admin = os.environ.get("SCUT_ADMIN_ONLY", "0") == "1"
    
    if is_admin:
        print(f"🔐 Admin Service Started on {host}:{port}")
    else:
        print(f"🚀 Backend Service Started on {host}:{port} (Multi-User Supported)")
        
    app.run(host=host, port=port, threaded=True)