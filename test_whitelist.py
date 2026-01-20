
import threading
import time
import os

# Mock lock if not imported, but we are running standalone, so we need to define context
import sys

# We need to test the logic functions. Since they are in server.py, we can try to import them if possible,
# or better yet, extract the logic to test it in isolation to avoid starting the whole flask app.
# However, for a quick verification script, I will copy the logic functions here to test them as units,
# simulating the environment.

ALLOWLIST_FILE = "test_allowed_users.txt"
ALLOWLIST_LOCK = threading.Lock()

def check_whitelist(username):
    allowlist_path = ALLOWLIST_FILE
    if not os.path.exists(allowlist_path):
        return False
    
    allowed = set()
    with ALLOWLIST_LOCK:
        with open(allowlist_path, "r", encoding="utf-8") as f:
            for line in f:
                # Logic from server.py
                s = line.split('#')[0].strip()
                if not s:
                    continue
                allowed.add(s)

    return str(username).strip() in allowed

def admin_add_user(username):
    allowlist_path = ALLOWLIST_FILE
    # Logic from server.py (simulated)
    try:
        username = str(username).strip()
        username = username.replace("\n", "").replace("\r", "")
        if not username:
             return "error: invalid username"

        with ALLOWLIST_LOCK:
            current_users = set()
            if os.path.exists(allowlist_path):
                with open(allowlist_path, "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.split('#')[0].strip()
                        if s:
                            current_users.add(s)
            
            if username in current_users:
                 return "error: user exists"

            with open(allowlist_path, "a", encoding="utf-8") as f:
                # Ensure newline if file not empty? server.py logic: f.write(f"\n{username}")
                # Ideally check if file ends with newline, but append usually just appends.
                # server.py used: f.write(f"\n{username}")
                f.write(f"\n{username}")
            
        return "success"
    except Exception as e:
        return str(e)

# --- Tests ---

def clean_up():
    if os.path.exists(ALLOWLIST_FILE):
        os.remove(ALLOWLIST_FILE)

def test_inline_comments():
    print("Test 1: Inline Comments")
    clean_up()
    with open(ALLOWLIST_FILE, "w", encoding="utf-8") as f:
        f.write("user1 # valid user\n")
        f.write("user2#another comment\n")
        f.write("# commented user\n")
        f.write("  user3  # spaces \n")

    assert check_whitelist("user1") == True
    assert check_whitelist("user2") == True
    assert check_whitelist("user3") == True # should handle strip
    assert check_whitelist("commented user") == False
    print("[PASS] Passed")

def test_input_sanitization():
    print("Test 2: Input Sanitization")
    clean_up()
    
    # 1. Normal add
    res = admin_add_user("testuser")
    assert res == "success"
    assert check_whitelist("testuser") == True
    
    # 2. Duplicate add
    res = admin_add_user("testuser")
    assert "exists" in res
    
    # 3. Newline injection
    res = admin_add_user("baduser\notheruser")
    assert res == "success" # It strips newline, so it adds "baduserotheruser"
    assert check_whitelist("baduserotheruser") == True
    assert check_whitelist("baduser") == False
    assert check_whitelist("otheruser") == False
    
    # 4. Empty after strip
    res = admin_add_user("   ")
    assert "error" in res
    
    print("[PASS] Passed")

def test_concurrency():
    print("Test 3: Concurrency")
    clean_up()
    
    def worker(i):
        admin_add_user(f"concurrent_user_{i}")
        
    threads = []
    for i in range(50):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    # Check count
    count = 0
    with open(ALLOWLIST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip(): count += 1
            
    # Note: server.py appends \n{username}, so if file starts empty, first line might be empty or formatted weird depending on impl.
    # But logic: f.write(f"\n{username}")
    # If file empty, it writes "\nusername".
    # Checking logic should handle empty lines.
    
    print(f"Total lines: {count}") 
    # Logic effectively adds 50 users.
    # verify read
    assert check_whitelist("concurrent_user_10") == True
    print("[PASS] Passed")

if __name__ == "__main__":
    test_inline_comments()
    test_input_sanitization()
    test_concurrency()
    clean_up()
    print("[SUCCESS] All Tests Passed")
