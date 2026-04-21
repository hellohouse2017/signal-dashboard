#!/bin/bash
# 選股策略 — Mini 一鍵安裝腳本
# 在 Mini 上執行：bash scanner/setup_mini.sh
set -e

CURRENT_USER=$(whoami)
CURRENT_HOME=$(eval echo ~$CURRENT_USER)
REPO_DIR="$CURRENT_HOME/Documents/Antigravity/選股策略"
SCANNER_DIR="$REPO_DIR/scanner"

echo "============================================================"
echo "  選股策略 Mini 排程安裝"
echo "  使用者: $CURRENT_USER"
echo "  路徑: $REPO_DIR"
echo "============================================================"

# 0. 確認路徑
if [ ! -d "$SCANNER_DIR" ]; then
    echo "❌ 找不到 $SCANNER_DIR"
    echo "   請確認 repo 路徑"
    exit 1
fi
cd "$REPO_DIR"

# 1. Git pull 最新
echo ""
echo "[1/5] Git pull 最新程式碼..."
git pull

# 2. 確認 python3 + yfinance
echo ""
echo "[2/5] 檢查 Python 環境..."
python3 --version
python3 -c "import yfinance; print(f'yfinance {yfinance.__version__} ✅')" 2>/dev/null || {
    echo "⚠️ yfinance 未安裝，安裝中..."
    pip3 install yfinance
}

# 3. 設定 TG credentials
echo ""
echo "[3/5] TG 警報設定..."
if [ ! -f "$SCANNER_DIR/.env" ]; then
    cp "$SCANNER_DIR/.env.example" "$SCANNER_DIR/.env"
    echo "📝 已建立 scanner/.env，請填入 TG 資訊："
    echo "   TG_BOT_TOKEN=你的bot token"
    echo "   TG_CHAT_ID=你的chat id"
    echo ""
    read -p "   要現在編輯嗎？(y/n) " EDIT_NOW
    if [ "$EDIT_NOW" = "y" ]; then
        nano "$SCANNER_DIR/.env"
    fi
else
    echo "✅ scanner/.env 已存在"
fi

# 4. 首次 git add JSON
echo ""
echo "[4/5] Git add JSON 歷史資料..."
JSON_FILES="scanner/VIX歷史.json scanner/VIX9D歷史.json scanner/VIX3M歷史.json scanner/SMH歷史.json scanner/corporate_actions.json"
NEED_COMMIT=false
for f in $JSON_FILES; do
    if [ -f "$f" ]; then
        git add "$f" 2>/dev/null && NEED_COMMIT=true
    fi
done
if [ "$NEED_COMMIT" = true ]; then
    if ! git diff --cached --quiet; then
        git commit -m "📊 加入 VIX/SMH 歷史 JSON"
        git push
        echo "✅ JSON 已 commit + push"
    else
        echo "✅ JSON 已在 git 中"
    fi
else
    echo "✅ JSON 檔案已在 git 中"
fi

# 5. 安裝 crontab 排程（避免 launchd TCC 權限問題）
echo ""
echo "[5/5] 安裝 crontab 排程..."
CRON_MARKER="# 選股策略：Mini 每日數據更新"
CRON_CMD="30 6 * * * cd $REPO_DIR && git pull --rebase >> /tmp/data-updater.log 2>&1 && cd scanner && /usr/bin/python3 data_updater.py --git-push >> /tmp/data-updater.log 2>&1"

# 檢查是否已安裝
if crontab -l 2>/dev/null | grep -q "選股策略"; then
    echo "✅ crontab 排程已存在，更新中..."
    # 移除舊的，加入新的
    (crontab -l 2>/dev/null | grep -v "選股策略" | grep -v "data_updater"; echo "$CRON_MARKER"; echo "$CRON_CMD") | crontab -
else
    (crontab -l 2>/dev/null; echo "$CRON_MARKER"; echo "$CRON_CMD") | crontab -
fi
echo "✅ crontab 排程已安裝"
echo "   每天 06:30 自動更新數據 + git push"

# 6. 測試 dry-run
echo ""
echo "============================================================"
echo "  測試 dry-run..."
echo "============================================================"
cd "$SCANNER_DIR"
python3 data_updater.py --dry-run --no-alert

# 驗證
echo ""
echo "============================================================"
echo "  ✅ 安裝完成！"
echo "============================================================"
echo "  排程: 每天 06:30 (crontab)"
echo "  手動: cd $SCANNER_DIR && python3 data_updater.py --git-push"
echo "  查 log: cat /tmp/data-updater.log"
echo ""
echo "  目前 crontab:"
crontab -l | grep -A1 "選股策略" || echo "  ⚠️ 排程未找到"
