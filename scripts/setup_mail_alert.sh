#!/bin/bash
# setup_mail_alert.sh — 安装 msmtp + 配置 SMTP + 部署磁盘监控 cron
# 部署到服务器执行：sudo bash setup_mail_alert.sh
# 执行前把下方 SMTP_* 和 MAIL_TO 填入真实值。

set -euo pipefail

# ================= 部署前必须填写 =================
SMTP_HOST="smtp.example.com"     # SMTP 服务器地址（如 smtp.exmail.qq.com / smtp.qq.com）
SMTP_PORT="465"                  # 端口：465(SSL) / 587(STARTTLS)
SMTP_USER="ops@example.com"      # 发件账号（完整邮箱）
SMTP_PASS="__AUTH_CODE__"        # 授权码/密码
MAIL_TO="admin@example.com"      # 收件邮箱（可逗号分隔多个）
# =================================================

APP_DIR="/home/admin/batch-product-studio"
MONITOR_SCRIPT="$APP_DIR/scripts/disk_monitor.sh"

echo "=== 1. 安装 msmtp ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq msmtp msmtp-mta

echo "=== 2. 写 /etc/msmtprc ==="
cat > /etc/msmtprc <<EOF
# 默认账户
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        alert
host           ${SMTP_HOST}
port           ${SMTP_PORT}
from           ${SMTP_USER}
user           ${SMTP_USER}
password       ${SMTP_PASS}

account default : alert
EOF
chmod 600 /etc/msmtprc

echo "=== 3. 测试发信 ==="
printf "Subject: [磁盘告警] 测试邮件\n\n这是一封来自 %s 的 msmtp 测试邮件。\n" "$(hostname)" \
  | msmtp "$MAIL_TO" && echo "✅ 测试邮件已发送至 $MAIL_TO"

echo "=== 4. 把收件/发件信息写回监控脚本 ==="
sed -i "s|__MAIL_TO__|${MAIL_TO}|" "$MONITOR_SCRIPT"
sed -i "s|__MAIL_FROM__|${SMTP_USER}|" "$MONITOR_SCRIPT"

echo "=== 5. 部署 cron（每天 08:00 检查）==="
CRON_LINE="0 8 * * * /bin/bash $MONITOR_SCRIPT >> /var/log/disk_monitor.log 2>&1"
( crontab -l 2>/dev/null | grep -v "disk_monitor.sh" ; echo "$CRON_LINE" ) | crontab -

echo "=== 6. 立即跑一次验证 ==="
/bin/bash "$MONITOR_SCRIPT" || true

echo "=== 完成 ==="
crontab -l | grep disk_monitor
