import requests
import json

# 使用一个有效的token（请从用户的最新登录中获取）
token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJqdGkiOiJmMzdjZGE5Ny1jMzU2LTQyNzItYjhkNS02ZGZhYTA4ZTZiNTMiLCJpYXQiOjE3Njk0MzMxODcsImV4cCI6MTc2OTUxOTU4NywidXNlckluZm8iOnsidXNlclJvbGVJZCI6MSwiYWNjb3VudCI6IjIwMjMyMDEwMDAzNCIsInNubyI6IjIwMjMyMDEwMDAzNCJ9LCJzZXgiOjEsInVzZXJJZCI6ODY1MzA5OTk2MzE1NDgxLCJzZXJpYWxWZXJzaW9uVUlEIjotNzcwMDgyNTkwNDgzMDcwNDYxOSwiaXNJbml0UGFzc3dvcmQiOmZhbHNlLCJwaG9uZSI6IiRzaWduOlNMcG5ZS050NVFOVzltc2N2eTVPMmc9PSIsIm5pY2tuYW1lIjoi5ZSQ6Ieq5by6IiwidGFnIjoicGMiLCJpc1JlYWxOYW1lIjp0cnVlLCJhY2NvdW50IjoiJHNpZ246TGJpamk4T1g4TXh2M3drSEhpenBDdz09In0.iLbvCeTwcbLwXOb64REgArtgfhkMxHB9cHKmTu9f17r1N7fNvO4zGB6maoJQz5Z-AQHXRFO-UfNRZd0IBPWDD7braVPQg-2YagTS1_schWRWJtUUYMfv70qUJnpJEDKkRaAMjygPKFc5yRE7qaef88R8W3K1VnijS-bq7AVVtH7vlx8y3le2FhYDwvgJSQipTh3YiRx69BumHXSW2WU0Q39DpHAXoZHhRKpUcBjKICF2bbezF5lL90aP6OJOgEpAmTfO0IL4gWrfmbOTkS8OZeah2y7Xb3j2kK-72GdarxZqyPOZIx3ROUv97POhtzxteaOELYlkHsgI_Nh3odHZiA"

headers = {
    'authorization': f'Bearer {token}',
    'content-type': 'application/json'
}

# 获取场地数据
response = requests.post(
    'https://venue.spe.scut.edu.cn/api/pc/order/rental/site/sessions',
    json={'stadiumId': 1, 'date': '2026-02-01', 'time': '18:00'},
    headers=headers
)

print("状态码:", response.status_code)
print("响应文本:", response.text[:500])

if response.status_code == 200:
    try:
        data = response.json()
        venues = {}
        for session in data.get('data', []):
            if 'venueName' in session:
                name = session['venueName']
                vid = session['venueId']
                venues[name] = vid
        
        print("\n场地ID映射:")
        print(json.dumps(venues, ensure_ascii=False, indent=2))
        
        # 按场地号排序（假设格式是"羽毛球场地X"）
        sorted_venues = {}
        for i in range(1, 17):
            for name, vid in venues.items():
                if f"场地{i}" in name or f" {i}" in name:
                    sorted_venues[str(i)] = str(vid)
                    break
        
        print("\n按编号排序的场地ID:")
        print(json.dumps(sorted_venues, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"解析失败: {e}")
