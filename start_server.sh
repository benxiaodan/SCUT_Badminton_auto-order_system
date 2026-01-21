#!/bin/bash
echo "Starting Production Server..."
# 确保加载生产环境配置
cp -f .env.production .env

# 使用 nohup 后台运行 (可选)
# nohup python3 server.py > server.log 2>&1 &

# 直接运行
python3 server.py
