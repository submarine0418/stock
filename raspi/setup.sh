#!/bin/bash
# 樹莓派一鍵設定腳本
# 用法：bash setup.sh

set -e

echo "=== 台股分析通知系統 - 樹莓派設定 ==="
echo ""

# 1. 確認 Python
if ! command -v python3 &> /dev/null; then
    echo "安裝 Python3..."
    sudo apt-get update && sudo apt-get install -y python3 python3-pip
fi

# 2. 確認 git
if ! command -v git &> /dev/null; then
    echo "安裝 git..."
    sudo apt-get update && sudo apt-get install -y git
fi

# 3. Clone repo（如果還沒有的話）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo "Repo 路徑：$REPO_DIR"
echo ""

# 4. 建立 config.json（如果不存在）
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "建立 config.json..."
    python3 "$SCRIPT_DIR/notify.py" 2>/dev/null || true
    echo ""
    echo "⚠ 請編輯 $SCRIPT_DIR/config.json"
    echo "  填入 Telegram Bot Token 和 Chat ID"
    echo ""
    echo "  取得方式："
    echo "  1. Telegram 搜尋 @BotFather → 輸入 /newbot → 照步驟做 → 拿到 token"
    echo "  2. Telegram 搜尋 @userinfobot → 它會回你的 chat_id"
    echo "  3. repo_path 填：$REPO_DIR"
    echo ""
    read -p "設定好了嗎？(y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "請設定好 config.json 後重新執行 setup.sh"
        exit 0
    fi
fi

# 5. 測試 Telegram
echo "測試 Telegram 連線..."
python3 "$SCRIPT_DIR/notify.py" --test

# 6. 設定 cron
echo ""
echo "設定排程..."

# 每天 08:35 檢查推送（比 GitHub Actions 晚 5 分鐘）
CRON_NOTIFY="35 8 * * 1-5 cd $REPO_DIR && python3 raspi/notify.py >> /tmp/stock_notify.log 2>&1"
# 星期六 10:05 檢查週回顧
CRON_WEEKLY="5 10 * * 6 cd $REPO_DIR && python3 raspi/notify.py >> /tmp/stock_notify.log 2>&1"
# 每 30 分鐘檢查一次（補漏，GitHub Actions 有時候會延遲）
CRON_CATCHUP="*/30 8-10 * * 1-6 cd $REPO_DIR && python3 raspi/notify.py >> /tmp/stock_notify.log 2>&1"

# 寫入 crontab（不重複）
(crontab -l 2>/dev/null | grep -v "stock_notify" ; echo "$CRON_NOTIFY" ; echo "$CRON_WEEKLY" ; echo "$CRON_CATCHUP") | crontab -

echo "✅ Cron 排程已設定："
echo "  - 週一到週五 08:35 推送每日分析"
echo "  - 週六 10:05 推送週回顧"
echo "  - 08:00-10:00 每 30 分鐘補檢查"

# 7. 設定 web server（systemd service）
echo ""
read -p "要啟動 Web Server 嗎？（手機瀏覽器看完整報告）(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    WEB_PORT=$(python3 -c "import json; print(json.load(open('$SCRIPT_DIR/config.json'))['web_port'])" 2>/dev/null || echo "8080")

    sudo tee /etc/systemd/system/stock-web.service > /dev/null << EOF
[Unit]
Description=台股分析 Web Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/notify.py --serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable stock-web.service
    sudo systemctl start stock-web.service

    # 取得 IP
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "✅ Web Server 已啟動"
    echo "   手機瀏覽器打開：http://$IP:$WEB_PORT"
fi

echo ""
echo "=== 設定完成！==="
echo ""
echo "手動測試：python3 $SCRIPT_DIR/notify.py"
echo "查看 log：cat /tmp/stock_notify.log"
echo "查看 cron：crontab -l"
