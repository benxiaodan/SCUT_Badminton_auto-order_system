@echo off
echo Starting Local Development Server...
echo Loading .env.development...

REM 设置使用 .env.development 作为配置文件 (通过 python-dotenv 的 override 或简单的 copy)
REM 由于 python-dotenv 默认读 .env，我们在启动前临时覆盖 .env 或者直接设置环境变量

copy /Y .env.development .env > nul

python server.py
pause
