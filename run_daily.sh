#!/bin/bash
# Wrapper invoked by the LaunchAgent a few times a day (see
# com.aptsearch.daily.plist). Runs the full scrape via the project venv and
# appends timestamped output to logs/daily.log.
cd "$(dirname "$0")" || exit 1
mkdir -p logs
echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >> logs/daily.log
.venv/bin/python track.py daily >> logs/daily.log 2>&1
echo "" >> logs/daily.log
