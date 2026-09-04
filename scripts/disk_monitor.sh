#!/bin/bash
# disk_monitor.sh — batch-product-studio 磁盘监控告警
# 部署到服务器：/home/admin/batch-product-studio/scripts/disk_monitor.sh
# cron 每天运行一次：0 8 * * *  （早 8 点检查）
#
# 双重检查：
#   1. 应用层告警文件 data/storage_alert.json（由 storage_maintenance 每 6h 写入）
#   2. 直接 du 磁盘占用（兜底，即使应用层没写 alert 文件也能发现）
# 任一项超阈值即发邮件。

set -u

APP_DIR="/home/admin/batch-product-studio"
ALERT_JSON="$APP_DIR/data/storage_alert.json"
STORAGE_DIR="$APP_DIR/storage"

# ---- 阈值（与 app/config.py 保持一致）----
WARN_MB=8192      # storage 告警阈值（80% of 10GB）
MAX_MB=10240      # storage 上限
DISK_WARN_PCT=80  # 整盘使用率告警阈值

# ---- 邮件配置（部署时填写）----
MAIL_TO="__MAIL_TO__"
MAIL_FROM="__MAIL_FROM__"
SUBJECT_PREFIX="[batch-product-studio 磁盘告警]"

HOSTNAME=$(hostname)
NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')

# ---- 采集数据 ----
STORAGE_MB=$(du -sm "$STORAGE_DIR" 2>/dev/null | cut -f1)
STORAGE_MB=${STORAGE_MB:-0}

DISK_USED_PCT=$(df -P / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
DISK_USED_PCT=${DISK_USED_PCT:-0}

DISK_AVAIL=$(df -h / 2>/dev/null | awk 'NR==2 {print $4}')

# 读应用层告警状态（若存在）
ALERT_RATIO=""
if [ -f "$ALERT_JSON" ]; then
    ALERT_RATIO=$(python3 -c "import json;d=json.load(open('$ALERT_JSON'));print(f\"{d.get('ratio',0)*100:.1f}%\")" 2>/dev/null)
    ALERT_OVER_WARN=$(python3 -c "import json;d=json.load(open('$ALERT_JSON'));print('yes' if d.get('over_warn') else 'no')" 2>/dev/null)
    ALERT_OVER_MAX=$(python3 -c "import json;d=json.load(open('$ALERT_JSON'));print('yes' if d.get('over_max') else 'no')" 2>/dev/null)
else
    ALERT_OVER_WARN="no"
    ALERT_OVER_MAX="no"
fi

# ---- 判定 ----
ALARM=0
REASONS=""

# 1) storage 目录超告警阈值
if [ "$STORAGE_MB" -ge "$WARN_MB" ]; then
    ALARM=1
    REASONS="${REASONS}- storage 目录占用 ${STORAGE_MB}MB >= 告警阈值 ${WARN_MB}MB\n"
fi

# 2) storage 目录超上限（应用层会拒绝新上传）
if [ "$STORAGE_MB" -ge "$MAX_MB" ]; then
    ALARM=1
    REASONS="${REASONS}- storage 目录占用 ${STORAGE_MB}MB >= 上限 ${MAX_MB}MB（应用已拒绝新上传）\n"
fi

# 3) 应用层告警文件标记 over_warn
if [ "$ALERT_OVER_WARN" = "yes" ]; then
    ALARM=1
    REASONS="${REASONS}- 应用层告警：占用比 ${ALERT_RATIO:-?} 已达告警线\n"
fi

# 4) 整盘使用率过高
if [ "$DISK_USED_PCT" -ge "$DISK_WARN_PCT" ]; then
    ALARM=1
    REASONS="${REASONS}- 整盘使用率 ${DISK_USED_PCT}% >= ${DISK_WARN_PCT}%（剩余 ${DISK_AVAIL}）\n"
fi

# ---- 输出 & 告警 ----
if [ "$ALARM" -eq 1 ]; then
    BODY="磁盘告警 @ ${HOSTNAME}
时间：${NOW}
storage 目录：${STORAGE_MB}MB / 上限 ${MAX_MB}MB（告警线 ${WARN_MB}MB）
应用层占用比：${ALERT_RATIO:-未知}
整盘使用率：${DISK_USED_PCT}%（剩余 ${DISK_AVAIL}）

触发原因：
${REASONS}

建议：登录后台「图片管理」归档后删除历史任务，或登录服务器手动清理：
  du -sh ${STORAGE_DIR}
"

    echo "$BODY"

    # 发邮件（若已配置）
    if [ "$MAIL_TO" != "__MAIL_TO__" ] && command -v msmtp >/dev/null 2>&1; then
        printf "From: %s\nTo: %s\nSubject: %s %s\n\n%s\n" \
            "$MAIL_FROM" "$MAIL_TO" "$SUBJECT_PREFIX" "$HOSTNAME" "$BODY" | msmtp "$MAIL_TO"
        echo "[mail] 已发送告警邮件至 $MAIL_TO"
    else
        echo "[mail] 未发送（MAIL_TO 未配置或 msmtp 未安装）"
    fi
    exit 1
else
    echo "OK @ ${NOW} | storage=${STORAGE_MB}MB/${MAX_MB}MB | 整盘=${DISK_USED_PCT}%（剩 ${DISK_AVAIL}）| 应用占用比=${ALERT_RATIO:-?}"
    exit 0
fi
