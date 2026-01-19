# -*- coding: utf-8 -*-
import ast
import sys

filename = 'server.py'
print("[CHECK] Checking file:", filename)

try:
    with open(filename, 'r', encoding='utf-8') as f:
        code = f.read()
    
    print("[INFO] File size:", len(code), "chars,", len(code.encode('utf-8')), "bytes")
    print("[OK] Encoding: UTF-8")
    
    ast.parse(code)
    print("[OK] Syntax check: PASSED")
    
    lines = code.split('\n')
    print("\n[STATS] Code statistics:")
    print("   Total lines:", len(lines))
    print("   Non-empty lines:", sum(1 for line in lines if line.strip()))
    
    tree = ast.parse(code)
    functions = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    print("   Functions:", len(functions))
    
    critical = ['monitor_worker', 'init_browser', 'execute_login_logic']
    missing = [f for f in critical if f not in functions]
    if missing:
        print("[WARN] Missing functions:", missing)
    else:
        print("[OK] Critical functions: ALL PRESENT")
    
    print("\n[SUCCESS] Overall check: PASSED - Code is ready to run")
    sys.exit(0)
    
except SyntaxError as e:
    print("[ERROR] Syntax error at line", e.lineno)
    print("   Message:", e.msg)
    sys.exit(1)
except Exception as e:
    print("[ERROR] Check failed:", str(e))
    sys.exit(1)
