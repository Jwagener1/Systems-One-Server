#!/bin/sh
# Runs report.py on schedule inside the container.
# Offline check: every OFFLINE_CHECK_INTERVAL_MINUTES (default 20)
# Daily report:  every day at DAILY_REPORT_HOUR (default 06:00 SAST)
# Monthly report: 1st of each month at MONTHLY_REPORT_HOUR (default 06:30 SAST)

OFFLINE_INTERVAL=${OFFLINE_CHECK_INTERVAL_MINUTES:-20}
DAILY_HOUR=${DAILY_REPORT_HOUR:-6}
MONTHLY_HOUR=${MONTHLY_REPORT_HOUR:-6}

last_offline=0
last_upload_check=0
last_daily_date=""
last_monthly_month=""
last_baseline_week=""

echo "S1 Reporter starting — offline check every ${OFFLINE_INTERVAL}min, daily at ${DAILY_HOUR}:00, monthly on 1st at ${MONTHLY_HOUR}:30, baselines every Sunday at 02:00"

while true; do
    now=$(date +%s)
    hour=$(date +%H | sed 's/^0//')
    minute=$(date +%M | sed 's/^0//')
    day=$(date +%d | sed 's/^0//')
    date_str=$(date +%Y-%m-%d)
    month_str=$(date +%Y-%m)
    week_str=$(date +%Y-%W)
    dow=$(date +%u)   # 1=Mon ... 7=Sun

    # Offline check — every OFFLINE_INTERVAL minutes
    elapsed=$(( now - last_offline ))
    if [ "$elapsed" -ge $(( OFFLINE_INTERVAL * 60 )) ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] Running offline check..."
        python3 /app/report.py offline
        last_offline=$now
    fi

    # Upload failure check — every OFFLINE_INTERVAL minutes (same cadence)
    elapsed_upload=$(( now - last_upload_check ))
    if [ "$elapsed_upload" -ge $(( OFFLINE_INTERVAL * 60 )) ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] Running upload check..."
        python3 /app/upload_monitor.py check
        last_upload_check=$now
    fi

    # Daily report — once per day at DAILY_HOUR:00
    if [ "$hour" -eq "$DAILY_HOUR" ] && [ "$minute" -lt 2 ] && [ "$date_str" != "$last_daily_date" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] Running daily report..."
        python3 /app/report.py daily
        last_daily_date=$date_str
    fi

    # Monthly report — 1st of month at MONTHLY_HOUR:30
    if [ "$day" -eq 1 ] && [ "$hour" -eq "$MONTHLY_HOUR" ] && [ "$minute" -ge 30 ] && [ "$minute" -lt 32 ] && [ "$month_str" != "$last_monthly_month" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] Running monthly report..."
        python3 /app/report.py monthly
        last_monthly_month=$month_str
    fi

    # Weekly baseline recompute — every Sunday at 02:00
    if [ "$dow" -eq 7 ] && [ "$hour" -eq 2 ] && [ "$minute" -lt 2 ] && [ "$week_str" != "$last_baseline_week" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] Running weekly baseline recompute..."
        python3 /app/compute_baselines.py
        last_baseline_week=$week_str
    fi

    sleep 60
done
