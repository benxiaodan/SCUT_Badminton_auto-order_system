"""
配置文件示例 - 请复制此文件为 config.py 并填入真实值
此文件仅作为参考模板，不包含真实敏感信息
"""
import os

# ============= SMTP 邮件配置 =============
# 邮件服务器地址
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
# 邮件服务器端口
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
# 发件人邮箱地址
SMTP_SENDER = os.getenv("SMTP_SENDER", "your_email@qq.com")
# 邮箱 SMTP 授权码（非登录密码）
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "your_smtp_auth_code")
