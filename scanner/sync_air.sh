#!/bin/bash
# 選股策略 — Air 開機同步腳本
# Mini 每天 06:30 更新 JSON + git push
# Air 開機時自動 git pull + 補本地 DB
set -e

REPO_DIR="/Users/tangyukao/Documents/Antigravity/選股策略"
SCANNER_DIR="$REPO_DIR/scanner"
LOG="/tmp/signal-air-sync.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') Air sync 開始" >> "$LOG"

cd "$REPO_DIR"

# 1. Git pull（取得 Mini push 的 JSON 更新）
if git pull --ff-only >> "$LOG" 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') git pull 成功" >> "$LOG"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️ git pull 失敗（可能有本地修改）" >> "$LOG"
    # 嘗試 stash + pull
    git stash >> "$LOG" 2>&1 || true
    git pull >> "$LOG" 2>&1 || true
    git stash pop >> "$LOG" 2>&1 || true
fi

# 2. 更新本地 DB（TWSE + yfinance DB，不動 JSON、不 push）
cd "$SCANNER_DIR"
python3 data_updater.py --twse-only --no-alert >> "$LOG" 2>&1 || true

echo "$(date '+%Y-%m-%d %H:%M:%S') Air sync 完成" >> "$LOG"
