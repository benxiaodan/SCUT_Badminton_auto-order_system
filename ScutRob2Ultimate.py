import calendar
import datetime
import threading
import time
from typing import List, Tuple

import requests

def month_weekday_timestamps(year: int, month: int, weekday: int) :
    """
    返回字典，键为当月所有指定“周几”对应的日期字符串 "YYYY-MM-DD"，值为该日 00:00:00（UTC+8）的毫秒级时间戳。

    参数：
      year: 年份，例如 2025
      month: 月份 1–12，例如 4
      weekday: 周几，1=Monday … 7=Sunday
    """
    if not 1 <= weekday <= 7:
        raise ValueError("weekday 必须在 1 到 7 之间，1=Monday…7=Sunday")

    # 把 weekday 转成 calendar.monthcalendar 的索引（0=Mon…6=Sun）
    wd_index = weekday - 1
    weeks = calendar.monthcalendar(year, month)

    # 构造一个 UTC+8 的 tzinfo
    tz_utc8 = datetime.timezone(datetime.timedelta(hours=8))

    result: dict[str, int] = {}
    for week in weeks:
        day = week[wd_index]
        if day:
            # 当天 00:00:00，带上 UTC+8 时区
            dt = datetime.datetime(year, month, day, 0, 0, 0, tzinfo=tz_utc8)
            ms = int(dt.timestamp() * 1000)
            key = f"{year:04d}-{month:02d}-{day:02d}"
            result[key] = ms

    return result

def sendApply(auth,year,month,week):
 t = threading.current_thread()
 url = "https://venue.spe.scut.edu.cn/api/pc/order/rental/orders/apply"

 headers = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ja;q=0.5",
    "authorization": f"Bearer {auth}",
    "content-type": "application/json",
    "origin": "https://venue.spe.scut.edu.cn/vb-user",
    "priority": "u=1, i",
    "referer": "https://venue.spe.scut.edu.cn/vb-user/booking",
    "sec-ch-ua": "\"Microsoft Edge\";v=\"135\", \"Not-A.Brand\";v=\"8\", \"Chromium\";v=\"135\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
 }
 res:dict[str,int]= month_weekday_timestamps(year,month,week)
 receipts=len(res)*40
 last_value = list(res.values())[-1] if res else None
 payload = {
    "userId":  865309996315481,
    "receipts": receipts,
    "buyerSource": 4,
    "stadiumId": 1,
    "mode": "week",
    "rentals": [
        {
            "belongDate": last_value,
            "week":week,
            #"start": "18:00",
            #"end": "20:00",
            "start": "20:00",
            "end": "22:00",
            "venueId": None
        }
    ]
 }
 labels = [
     # 511508061201852,511589859434853,511589859434854,511589859434855,511839951512856,511942956511857,512037093039858,512160523250859,
     # 512288707374860,512382636613861,51246724428862,512536644146863,51262484178864,512719988472865,5128057837866,512885983484867
 ]

 

 # venue_ids = [    8-10
 #     511508061201884,511589859434885,511589859434886,511589859434887,511839951512888,511942956511889,512037093039890,512160523250891,
 #     512288707374892,512382636613893,51246724428894,512536644146895,51262484178896,512719988472897,5128057837898,512885983484899,
 # ]
 venue_ids = [
     511839951512888,
 ]
 for vid in venue_ids:
     payload["rentals"][0]["venueId"] = vid
     response = requests.post(url, headers=headers, json=payload)
     print(f"状态码: {response.status_code}")
     print("响应内容:")
     try:
         print(response.json())
     except:
         print(response.text)

# 每个任务线程的循环体：不断预约，直到程序被终止
# ⬇⬇⬇ 把这段配置加到文件最开头（import 下面） ⬇⬇⬇
# ================= 定时配置区域 =================
# 设置你想开始抢票的时间（24小时制）
TARGET_HOUR = 18  # 目标时 (例如早上 6 点)
TARGET_MINUTE = 0  # 目标分
TARGET_SECOND = 0  # 目标秒


# ===============================================

# ... (中间的 sendApply 等函数保持不变) ...

# ⬇⬇⬇ 替换原本的 reservation_worker 函数 ⬇⬇⬇
def reservation_worker(auth: str, year: int, month: int, week: int):
    t = threading.current_thread()
    print(f"[{t.name}] 线程已就绪，正在等待目标时间 {TARGET_HOUR}:{TARGET_MINUTE:02d}:{TARGET_SECOND:02d} ...")

    # === 1. 定时等待逻辑 ===
    while True:
        now = datetime.datetime.now()
        # 构造今天的目标时间点
        target_time = now.replace(hour=TARGET_HOUR, minute=TARGET_MINUTE, second=TARGET_SECOND, microsecond=0)

        # 如果当前时间已经晚于目标时间（比如现在6:01，目标是6:00），说明是第二天抢，或者立即开抢
        # 这里逻辑是：如果还没到今天6点，就等今天6点。如果过了今天6点，就认为是测试，直接开抢
        # 或者你可以改成：target_time += datetime.timedelta(days=1) 等明天

        # 计算剩余秒数
        diff = (target_time - now).total_seconds()

        if diff <= 0:
            # 时间到了（或者已经过了），跳出等待循环，开始抢票
            print(f"[{t.name}] ⏰ 时间到！全力开火！！！")
            break

        # 智能休眠策略
        if diff > 5:
            time.sleep(diff - 2)  # 还有很久，睡大觉
        elif diff > 0.5:
            time.sleep(0.1)  # 快到了，小睡
        else:
            pass  # 最后0.5秒，死循环空转（极速响应）

    # === 2. 原有的抢票循环 ===
    print(f"[{t.name}] 启动自动预约任务")
    while True:
        try:
            sendApply(auth, year, month, week)
        except Exception as e:
            print(f"[{t.name}] 出错: {e}")
        # time.sleep(interval_sec) # 保持原样，注释掉sleep以求最快速度

def main():
    # 每个任务的参数：auth, year, month, week
    jobs: List[Tuple[str, int, int, int]] = [
         # ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJhY2UxNzJkNC0zMWVlLTQxZjgtOTBmMC0zOTdmMTg1ZDk1MDgiLCJpYXQiOjE3NDg2ODI0NjcsImV4cCI6MTc0ODc2ODg2NywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjM2MjM2MDEzMSIsInNubyI6IjIwMjM2MjM2MDEzMSJ9LCJzZXgiOjEsInVzZXJJZCI6NjI1MzgzNjg5NDcxMjk3LCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOndDTEtLOUtYYnNNMjBlOUQyTWVYYUE9PSIsIm5pY2tuYW1lIjoi5ZC05piV5rKFIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246bVVaTzhacGZIOFhLYUl4RW1uNHRpZz09In0.mmyNL6oVO5WWIo3Z9ZtqAAYxA1VOEVsn-TtZVjkPr-Inj2tLf5VZw3lBGbHGWeBhTwpFV8qt3j0W-c8VP9rGAo5RZB6Kxsw_YiEXO99RKLh0XtiZHeSqjtC872NEgElR3j_8lxcvPTAAG7c5Dr7d1qQNlk_cEmMrk6ds6fPY0eqJ8BeydA9besRZzXtg0zp8mlXgFOlJhHfjCd3D9QKcdOPHyI54qETgtJCh0Ky482yp0dJAsD8Sjj_Y90ra5MI7o3kQZT6M2rJGTwmPAIVaoU3lJ0S-3KorAls-6VYyKz89YUAfFTVWqlBikTjLgWoFJDTX4R8ZSS_PGzJ8hAi0Cg",2025, 6, 7),#wxy
         # ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiI5YzJmNDA0Yy05NzYwLTQ0MjAtYmY3Yy0yODI1OTYxODE3MzQiLCJpYXQiOjE3NDg2ODQyMjQsImV4cCI6MTc0ODc3MDYyNCwidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjIyMDEwMTE4NyIsInNubyI6IjIwMjIyMDEwMTE4NyJ9LCJzZXgiOjEsInVzZXJJZCI6NjIzMTMxMTMyNzgyOTEsInNlcmlhbFZlcnNpb25VSUQiOi03NzAwODI1OTA0ODMwNzA0NjE5LCJpc0luaXRQYXNzd29yZCI6ZmFsc2UsInBob25lIjoiJHNpZ246Uk5ncGZTOU5RdU9raCtiRllsNUJlQT09Iiwibmlja25hbWUiOiLlj7blrZDnq4siLCJ0YWciOiJwYyIsImlzUmVhbE5hbWUiOnRydWUsImFjY291bnQiOiIkc2lnbjpzcDFvNXBIK2dVSGdZQTY1UmJZUThnPT0ifQ.VG1RRA9jY6r_7CR5ASDYEYnO0JePQJB39x4pVzsQvuoJq72bypL49wWfLSNToyDt4gBNYG4IVLfCCqFfReF0SSY-nxZSnFqKjtNcwfamF84XEoZrWmSj-v4TkMnZlbhIgMAICrTVnUhtinR9aS5zlas0yJ_I1U8LG3YSAqHm5e8LMIajUB1GNf-jfiWV6rjNGjlw96OWtPfLsZOeKsvDr4edtCwr2kg0cGOVCAWCy0lp4XlvryHgVPfPMfuY8mQ_Hi5uqoBkAMNIViG6pqIlodyMqHSlNIB0BDXoYaofc3KX3MWDnxZrer0v6EezPHsY3qGRI3TijhWjDmlj2iR4PA", 2025, 6, 2),#小叶哥
         # ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJmNjRkMjc1OC00ZDAzLTRhMzMtOTExMi1lNTViNmIwY2FjYjUiLCJpYXQiOjE3NDg2ODM4MjcsImV4cCI6MTc0ODc3MDIyNywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMTAwMTU0MyIsInNubyI6IjIwMjMyMTAwMTU0MyJ9LCJzZXgiOjEsInVzZXJJZCI6ODgwNDc3NzE3NDMyNzMzLCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOkh3VTBzSjAreDRMWTZZTGd4QWpIUXc9PSIsIm5pY2tuYW1lIjoi54aK5b-X6IGqIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246bnphblQyZHAyVkd1UDZmSENaV3UwQT09In0.OUKoX6ADS-_2oT716dt983GtJMqv7zte8PQm8_X9lsPoySQsd6qgjoTkvZ8g4esVQVbDbhfwiMp1FSg9G7tNACoXtBKeVkMC2_oG_v0GwipFF0Gfiqp0KWqIE2uOLLWn-p9LSZmxyEn6GOKPg4uDQWjUeh0WHUjeILHnvBDLgbjUH5LHw3zgeheeBqs3QJDQ5hlVHXzTpmaPj-S-ZWC3DGQ4LqWdUx73WhlDHKOaVQMQx440XiSdXmKG-YNpdGsY_d-qebg0bo-UvoHDPJ7MmIig8WW6fXJQQakoVqBSDvgxdN_PCGvZhbWZwQP5HaFzGpIVpbm0CqOBAyoh5JNQiw",2025, 6, 3),#笨小蛋
         #  ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiIwOTk5MjEyNy1hYjNiLTQzNzUtYTQ5OS01NDI0YTFkNDNjMGQiLCJpYXQiOjE3NjE5MDM2MzMsImV4cCI6MTc2MTk5MDAzMywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjQyMTAwMjU0MSIsInNubyI6IjIwMjQyMTAwMjU0MSJ9LCJzZXgiOjEsInVzZXJJZCI6NjM3MDE4MTk1NzY4MTEsInNlcmlhbFZlcnNpb25VSUQiOi03NzAwODI1OTA0ODMwNzA0NjE5LCJpc0luaXRQYXNzd29yZCI6ZmFsc2UsInBob25lIjoiJHNpZ246UElHZHlvTzV6Mis2TmZsanFVZ3p4dz09Iiwibmlja25hbWUiOiLlkLTlpZXnkosiLCJ0YWciOiJwYyIsImlzUmVhbE5hbWUiOnRydWUsImFjY291bnQiOiIkc2lnbjovUWEvSGRCaHdTM0FzQzd3YTBTWVNnPT0ifQ.pGvKYcZPRHP7QCsMU9M5J6TVfz7vHwdgpKVzsMK9lprxTtW5sXr3awWhk5-Q0NA7qg3EJptGMILkqbig-_kB0RmwPrh71u4-ftKOX04ZXk47VH2IuzYSYfTekcmM3hCsGiB0_cPNLixnp_FM8nGjTbWy7k7eFaVG6f_r1xbL6fyroHz77dtLG1GOv7E_9VOV_g1U5AKjjqeSoTkOr_6wO6I5tZWrnbDNakbZ8OMcXEkQFga6hWjwn-llUGmFHoa0_uvQJoVQ51UlSlTnzxw8-hQ7jmAOUWQfUKUHUb8ymM83jfxIHygPm3R-X0DOEs2sHQ9CRXFRaAgwXqjuuuUjmQ",2025, 11, 1),#wyz
         #  ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiIzNDNkMTJjMS0yYjVjLTRlZjktOGM2Mi0zMjEyY2RlMzVjZmIiLCJpYXQiOjE3NjE5MDM2ODMsImV4cCI6MTc2MTk5MDA4MywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMTAwMjM4NCIsInNubyI6IjIwMjMyMTAwMjM4NCJ9LCJzZXgiOjEsInVzZXJJZCI6NjM5NDg2MjYwODg1MzE0LCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOnpIOXkxV1AxOForOW9ZTzErMW4xWkE9PSIsIm5pY2tuYW1lIjoi5p2O5Yqb5L2zIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246ZEQ3NGNxdEFncEpvS0UrWFlHRUNwZz09In0.kQIzqEPB-LeGr8wRxuesWwEimkGEdFNp3FuGLSNSbcJPRzZSygVBigvAwstyTc-PnJgrOrj1g0DQf3d4cvb5jPob3QUw5mCyzMeGiMIUPNZucEixeHZURtHdMuBK3ZxNgBUUFoSWV8-Zsc7ouh0GKr_c463ibpBq3ypxkTdMUfc-p9wdPVrRIhbY6a9QFSHBpokbxbpzWNNmQgZQQv_FkQ7eWUQKEfc6sUYdmxMbiiIaobZNt9jGUbDi_XAzay6PGYyhmOBNuclnm7wLWpw6Y_7k9ttzJK9KJTzA3R4yzGrsNzNPfRkjGAWW1QEANY0vj0jnxhlRpVjYbMxvveRJ0w", 2025, 11, 7),#力佳
         #  ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiI1OTIzNjliMC1jZDRkLTQ1MzgtOTg0My0wMzc3M2IyNjExOGMiLCJpYXQiOjE3NjE5MDM3MzAsImV4cCI6MTc2MTk5MDEzMCwidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMDEwMDA2OCIsInNubyI6IjIwMjMyMDEwMDA2OCJ9LCJzZXgiOjEsInVzZXJJZCI6NjQ0MzkzMTUxNDk1MzE2LCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOlhpaGhONkNFc2QrSGRTQlErS1gvU0E9PSIsIm5pY2tuYW1lIjoi5YiY5rabIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246Q2sxdFNsbXllMHBhT1VEWGFTSG8vQT09In0.H6Zb9J7Hj7hMdr1h37c7edhSylVJzVTKfGa988Zljie0e4jx-rOXGl8oQL6pxPuOavDGlOEcedwlXykAeBfBKezhB3_S-16aRPkPVa0Z0Qc1HNbMXek7z6RUE0dBH14ufQaARbs1BPslLnNxA4amqEolIMpnDce10Fk2L7i10cg2mA3c7KOrb6dpQFl8lUZrDVGrJnnBfjuR4H71oBXrBIhLxstt891sfF5xT6iTlaBYdNooO4kzYLU0D8n8slKXzvSYfM1qaM9vVfv-R1GELcGZC99IUiQer2uOf_y10BzkjmleBDwOayXcmaHs4GJlGDCzdm5Wqa_Ps6ax-rb--w", 2025, 11, 6),#涛哥
         #  ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiI1NjMxMTgzNy03ODNkLTQ4MGQtOTc4My0xOGRjYTZiYWU3MGIiLCJpYXQiOjE3NjcwNjU3MzMsImV4cCI6MTc2NzE1MjEzMywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMDEwMDIyMCIsInNubyI6IjIwMjMyMDEwMDIyMCJ9LCJzZXgiOjEsInVzZXJJZCI6NjAxNTYxNzEzODE4MjQ0LCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOjVxZU5FWHdvNGhGYUR0V3FHN2xxb2c9PSIsIm5pY2tuYW1lIjoi6bqm5ZCv6b6ZIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246OU9qMU9SU0FDUTI5ZTJEYW5PdWFHQT09In0.Fj0Jff9EAbi-1_gh9UHPyLz5_rXOmbLVaDAPG02x-eVI_JLXC_tAEdDA7VmG6UDvJ_sKPNSzVjbwqokqcUGoKGLskiLXkNDPbK24TcPFGOZrbf1rpg2xXy_Vuf4m9PFc45qHPsPeyC1SqjKA6_pMitCfOdu2F-QBrpUv5ymNRMCVdE10XzWv4qEWXuR-9HPC0UAgNeV8X5NwDh1_ghihLZEcs-znuPefXc5Z5axVrFVhba45kDL85Uf3Ormitx9FRRKmYw5TJnCK-UxkGraJ3PDDejBSFgLqGbhmZ_YwWWiwLA6PX2no8GiT49n4J8ya92RLHk4KFjY6YQm5IseLaw",2025, 12 ,2)#龙哥
        ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiI2YjhjYmQyOC02ZTk4LTRlYjQtOGEwYS0xMDI5ODhjMjhkMjIiLCJpYXQiOjE3Njc4NTc5ODAsImV4cCI6MTc2Nzk0NDM4MCwidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMDEwMDAzNCIsInNubyI6IjIwMjMyMDEwMDAzNCJ9LCJzZXgiOjEsInVzZXJJZCI6ODY1MzA5OTk2MzE1NDgxLCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOlNMcG5ZS050NVFOVzltc2N2eTVPMmc9PSIsIm5pY2tuYW1lIjoi5ZSQ6Ieq5by6IiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246TGJpamk4T1g4TXh2M3drSEhpenBDdz09In0.NRWYmEOFT5pJlUDo4-1wwJbdMoFfHbFgPVW-bR4342kUhcNsFO13unCu6Frn4gZy2H_bY-rPMm84E1rVp1q4yNven3c9VtpB6xDIemnl6kenCabEfoAiCUOROstVLEB5EthJNUx_QOCmvF0TroXTPprmzMUElmj-YVKRK-RMCE-02VQZx3v9xfkq3Co7kFVM2MJhWyJPFsa0cttF5KrpVIDheAWrdwutIdfrAS6m3xsuEc1M8m1SnNycJ6QOKYgyvQbXl37UWMXTA4gmV0vLk23jA9L1zVNJYFL8Qw2KYD7Gy-pYAy2OUY8vZOd8n7M68qBwB0bAk2KvuRgZ6fpAqA",2026,1,8),#唐自强
        ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiIwMGQ3NTVhMy04YWMyLTQ2N2ItOGZjNi1lNzIyODA5ZGM5ZGMiLCJpYXQiOjE3NjcxNzEwNTksImV4cCI6MTc2NzI1NzQ1OSwidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjQyMTAwMzQ1OSIsInNubyI6IjIwMjQyMTAwMzQ1OSJ9LCJzZXgiOjEsInVzZXJJZCI6MTU0Njg3NzQxNjgyNzQzLCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOmU0Q1dSV3ZIa0tpMTFsNVpzRDN5VHc9PSIsIm5pY2tuYW1lIjoi5Lil5raM5rabIiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246R2gxRmE5Vzh0MW5MZmtDcW5IcVFOZz09In0.gH4OVecxImPh4fl5JYqKqeoyrnpOX0Z7QK69vOsHwyyHB-bB-P73_Q3ZvhSvq2jIl9nPOVSNdNx7tamkkqhpmm79aCIClfhITaK0RNU69jCunVGyqWNEaMM-EvxirKeFaUR1a91df31p-k6dK6DpoWTbwXpNZsVxTHk-86uaA34CSy7NIjJJt5pZMkW05dcKZkx2Li-vW9EU9WuY44VgUkJ3MalxQ_a5rvC6KCVztoEm2DuFKH4FgIJgsff10VkBVsGSuu2VjQmCzuhtqyrhvWqyTDalfsbSaEKdXKV4UMdb9Frxy3cEhNe2AezF6hJVc4kJoJshx-P0zsQxD8A_yw", 2026, 1,6),#师弟
        ("eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiI4Y2VkMjIwMy0yZDQ0LTQ0MWQtOWZmOS0yMjJkZmEzZWE0OTciLCJpYXQiOjE3NjcxNzExMDEsImV4cCI6MTc2NzI1NzUwMSwidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMTAwODY0NSIsInNubyI6IjIwMjMyMTAwODY0NSJ9LCJzZXgiOjEsInVzZXJJZCI6NzEyMzU0NDI5OTE2OSwic2VyaWFsVmVyc2lvblVJRCI6LTc3MDA4MjU5MDQ4MzA3MDQ2MTksImlzSW5pdFBhc3N3b3JkIjpmYWxzZSwicGhvbmUiOiIkc2lnbjpTdytnVmVhdHp3NzFQazRUZzVIUVdnPT0iLCJuaWNrbmFtZSI6IuiwouiQjOa1tyIsInRhZyI6InBjIiwiaXNSZWFsTmFtZSI6dHJ1ZSwiYWNjb3VudCI6IiRzaWduOlBXM3hZbSs3SEl5OFZaK0NqOXZHK3c9PSJ9.SEFeGWy3G6ZPISyYCt8vfR0bzZdxJonOdmvAnVBirM6ZM50w5J2vG5JMTBRO5ZofprEUQxkglk5uq6Yxf919CuwOCOb52V8aJN44ncbygSOZwPRf_AfnlWeVCWPpxabiMBQ3ruTFP7zl986JqjVJVqkrvG-1byhSB8mWr3tF1aMWo1Jqs6B4cBoB5LI0pXcZqWe7CfUhq6HtO5i__MA4nrWqkvRG59f1QtNtzKNiTRKpKDfEtcAKPeV373cj6JFhGuSvdU7cy1X0gcIyVRXRP_jc7G2cbR8ytac0Qmj2Th9DDzsYdGxjtftkaAGqIKW5Jj27xJdHy-WrbDt_euD5wg",2026,1,3),#谢萌海
        # ("token_GHI789", 2025, 4(月份）, 4（周几),
        # 可继续添加更多 token 和日期
    ]



    threads: List[threading.Thread] = []

    for auth, year, month, week in jobs:
        t = threading.Thread(
            target=reservation_worker,
            args=(auth, year, month, week),
            daemon=True,  # 主线程退出时自动关闭子线程
            name=f"AutoApply-{auth[-4:]}-{year}{month:02d}W{week}"
        )
        threads.append(t)
        t.start()
        print(f"已启动线程 {t.name}")

    print("自动预约系统已启动，按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(60)  # 主线程保持存活
    except KeyboardInterrupt:
        print("\n系统终止，正在退出...")


if __name__ == "__main__":
    main()