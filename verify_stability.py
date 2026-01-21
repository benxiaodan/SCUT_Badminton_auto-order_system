import server
import time
import sys
import threading
import os

def test_concurrency_and_cleanup():
    print("=== 开始稳定性测试 ===")
    
    # 手动设置 DRIVER_PATH 以防找不到 (server.py 会自动找，但这里显式一点也好，或者依赖自动)
    # 依赖自动即可。

    # 1. 启动 Driver 1
    print("Attempting to start Driver 1...")
    d1 = server.init_browser()
    if not d1:
        print("❌ Failed to start Driver 1")
        return
    print(f"✅ Driver 1 started. PID: {getattr(d1, '_pid', 'Unknown')}")
    
    # 2. 启动 Driver 2
    print("Attempting to start Driver 2...")
    d2 = server.init_browser()
    if not d2:
        print("❌ Failed to start Driver 2")
        server.close_driver(d1)
        return
    print(f"✅ Driver 2 started. PID: {getattr(d2, '_pid', 'Unknown')}")
    
    # 3. 测试并发限制 (Driver 3 应无法立即获取)
    print("Checking Semaphore limit...")
    # 尝试非阻塞获取，应该失败
    if server.BROWSER_SEMAPHORE.acquire(blocking=False):
        print("❌ Error: Semaphore limit failed! Acquired 3rd permit.")
        server.BROWSER_SEMAPHORE.release()
    else:
        print("✅ Semaphore limit verified. (Cannot acquire 3rd permit)")

    # 4. 测试释放
    print("Closing Driver 1...")
    server.close_driver(d1)
    
    # 现在应该能获取
    if server.BROWSER_SEMAPHORE.acquire(blocking=False):
        print("✅ Permit released correctly. Re-acquired for verification.")
        server.BROWSER_SEMAPHORE.release()
    else:
        print("❌ Error: Semaphore not released after closing driver!")

    # 5. 模拟残留清理
    # d2 仍然打开。我们退出脚本，atexit 应该会被触发。
    # 为了验证，我们打印当前 ACTIVE_DRIVER_PIDS
    print(f"Current Active PIDs: {server.ACTIVE_DRIVER_PIDS}")
    print("Exiting now. The cleanup_at_exit function should run and kill Driver 2.")
    
if __name__ == "__main__":
    test_concurrency_and_cleanup()
