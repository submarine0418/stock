#!/usr/bin/env python3
"""
樹莓派通知服務
1. git pull 拉最新的 observation.md
2. 檢查有沒有新的分析
3. 有的話透過 Telegram Bot 推送到手機
4. 同時跑一個簡易 web server 讓手機瀏覽器也能看

用法：
  python notify.py          # 單次執行（配合 cron）
  python notify.py --serve  # 啟動 web server（背景常駐）
  python notify.py --test   # 測試 Telegram 連線
"""
import os
import sys
import json
import subprocess
import re
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import urllib.request
import urllib.parse

# ── 設定（改成你自己的）──
CONFIG_FILE = Path(__file__).parent / 'config.json'

DEFAULT_CONFIG = {
    "telegram_bot_token": "",       # 從 @BotFather 拿
    "telegram_chat_id": "",         # 從 @userinfobot 拿
    "repo_path": "",                # 你的 stock repo 路徑，例如 /home/pi/stock
    "web_port": 8080,               # web server port
    "state_file": "~/.stock_notify_state",  # 記錄已推送的分析
}


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        # 補上預設值
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    else:
        # 第一次執行，建立設定檔
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"已建立設定檔：{CONFIG_FILE}")
        print("請編輯 config.json 填入 Telegram Bot Token 和 Chat ID")
        print()
        print("設定步驟：")
        print("1. Telegram 搜尋 @BotFather → /newbot → 取得 token")
        print("2. Telegram 搜尋 @userinfobot → 取得你的 chat_id")
        print("3. 填入 config.json")
        print(f"4. repo_path 填你的 stock repo 路徑")
        sys.exit(1)


def git_pull(repo_path):
    """拉最新的 repo"""
    try:
        result = subprocess.run(
            ['git', 'pull', '--rebase', 'origin', 'main'],
            cwd=repo_path, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f"  git pull OK: {result.stdout.strip()}")
            return True
        else:
            print(f"  git pull 失敗: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  git pull 錯誤: {e}")
        return False


def get_latest_entry(repo_path):
    """從 observation.md 取得最新一筆分析"""
    obs_file = os.path.join(repo_path, 'observation.md')
    if not os.path.exists(obs_file):
        return None, None

    with open(obs_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 切割每日區塊，取最後一個
    sections = re.split(r'(?=## \d{4}-\d{2}-\d{2})', content)
    sections = [s.strip() for s in sections if s.strip() and s.strip().startswith('## ')]

    if not sections:
        # 試試週回顧格式
        sections = re.split(r'(?=## 📊 週回顧)', content)
        sections = [s.strip() for s in sections if s.strip() and '週回顧' in s]

    if not sections:
        return None, None

    latest = sections[-1]
    # 用 hash 判斷是否已推送過
    entry_hash = hashlib.md5(latest.encode()).hexdigest()[:12]

    return latest, entry_hash


def get_summary_txt(repo_path):
    """讀取 summary.txt（精簡版，適合推播）"""
    for fname in ['summary.txt', 'weekly_summary.txt']:
        fpath = os.path.join(repo_path, fname)
        if os.path.exists(fpath):
            mtime = os.path.getmtime(fpath)
            # 只取 24 小時內的
            if (datetime.now().timestamp() - mtime) < 86400:
                with open(fpath, 'r', encoding='utf-8') as f:
                    return f.read().strip()
    return None


def was_already_sent(state_file, entry_hash):
    """檢查這筆分析是否已經推送過"""
    state_path = os.path.expanduser(state_file)
    if os.path.exists(state_path):
        with open(state_path, 'r') as f:
            return f.read().strip() == entry_hash
    return False


def mark_as_sent(state_file, entry_hash):
    state_path = os.path.expanduser(state_file)
    with open(state_path, 'w') as f:
        f.write(entry_hash)


# ── Telegram ──

def telegram_send(token, chat_id, text):
    """透過 Telegram Bot API 發送訊息"""
    # Telegram 限制 4096 字元
    if len(text) > 4000:
        text = text[:3900] + "\n\n...（完整版請看 web）"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': 'true',
    }).encode()

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get('ok'):
                print("  ✅ Telegram 推送成功")
                return True
            else:
                print(f"  ❌ Telegram 失敗: {result}")
                return False
    except Exception as e:
        # Markdown 解析失敗的話，改用純文字重送
        print(f"  Telegram Markdown 失敗，改用純文字: {e}")
        try:
            data = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': text,
                'disable_web_page_preview': 'true',
            }).encode()
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                if result.get('ok'):
                    print("  ✅ Telegram 推送成功（純文字）")
                    return True
        except Exception as e2:
            print(f"  ❌ Telegram 完全失敗: {e2}")
        return False


# ── Web Server ──

def start_web_server(repo_path, port):
    """啟動簡易 web server，提供 observation.md 的 HTML 版"""

    class StockHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/' or self.path == '/index.html':
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                html = generate_html(repo_path)
                self.wfile.write(html.encode('utf-8'))
            elif self.path == '/api/latest':
                self.send_response(200)
                self.send_header('Content-type', 'application/json; charset=utf-8')
                self.end_headers()
                entry, _ = get_latest_entry(repo_path)
                self.wfile.write(json.dumps({
                    'content': entry or '無資料',
                    'updated': datetime.now().isoformat()
                }, ensure_ascii=False).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 安靜模式

    server = HTTPServer(('0.0.0.0', port), StockHandler)
    print(f"🌐 Web server 啟動：http://0.0.0.0:{port}")
    server.serve_forever()


def generate_html(repo_path):
    """把 observation.md 轉成手機友善的 HTML"""
    obs_file = os.path.join(repo_path, 'observation.md')
    if os.path.exists(obs_file):
        with open(obs_file, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        content = "# 無資料"

    # 簡易 Markdown → HTML
    html_content = content
    html_content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_content)
    html_content = re.sub(r'^---$', r'<hr>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^- (.+)$', r'<li>\1</li>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'\n\n', r'<br><br>', html_content)

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>台股分析</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, 'PingFang TC', 'Microsoft JhengHei', sans-serif;
    background: #0d1117; color: #c9d1d9;
    padding: 16px; max-width: 800px; margin: 0 auto;
    font-size: 15px; line-height: 1.6;
  }}
  h1 {{ color: #58a6ff; font-size: 22px; margin: 16px 0 8px; }}
  h2 {{ color: #58a6ff; font-size: 18px; margin: 24px 0 8px; border-bottom: 1px solid #30363d; padding-bottom: 4px; }}
  h3 {{ color: #79c0ff; font-size: 16px; margin: 16px 0 4px; }}
  strong {{ color: #f0883e; }}
  hr {{ border: none; border-top: 1px solid #30363d; margin: 24px 0; }}
  li {{ margin-left: 20px; margin-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 13px; }}
  th, td {{ border: 1px solid #30363d; padding: 6px 8px; text-align: left; }}
  th {{ background: #161b22; color: #58a6ff; }}
  .updated {{ color: #8b949e; font-size: 12px; text-align: right; margin-top: 16px; }}
</style>
</head>
<body>
{html_content}
<div class="updated">更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</body>
</html>"""


# ── 主程式 ──

def main():
    cfg = load_config()

    if not cfg['telegram_bot_token'] or not cfg['telegram_chat_id']:
        print("❌ 請先在 config.json 設定 telegram_bot_token 和 telegram_chat_id")
        sys.exit(1)

    if not cfg['repo_path']:
        print("❌ 請先在 config.json 設定 repo_path（你的 stock repo 路徑）")
        sys.exit(1)

    # --test：測試 Telegram 連線
    if '--test' in sys.argv:
        print("測試 Telegram 推送...")
        telegram_send(
            cfg['telegram_bot_token'],
            cfg['telegram_chat_id'],
            "🧪 測試訊息\n台股分析通知系統連線成功！"
        )
        return

    # --serve：啟動 web server（背景常駐）
    if '--serve' in sys.argv:
        start_web_server(cfg['repo_path'], cfg['web_port'])
        return

    # 預設：單次執行（配合 cron）
    print(f"=== 通知檢查 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # 1. git pull
    print("拉取最新資料...")
    git_pull(cfg['repo_path'])

    # 2. 檢查有沒有新分析
    entry, entry_hash = get_latest_entry(cfg['repo_path'])
    if not entry:
        print("  沒有分析資料")
        return

    if was_already_sent(cfg['state_file'], entry_hash):
        print("  這筆已經推送過了，跳過")
        return

    # 3. 推送
    print("發現新分析，推送中...")

    # 優先用 summary.txt（精簡版）
    summary = get_summary_txt(cfg['repo_path'])
    if summary:
        telegram_send(cfg['telegram_bot_token'], cfg['telegram_chat_id'], summary)
    else:
        # 沒有 summary 就用 observation.md 的最新一筆
        telegram_send(cfg['telegram_bot_token'], cfg['telegram_chat_id'], entry)

    mark_as_sent(cfg['state_file'], entry_hash)
    print("完成！")


if __name__ == '__main__':
    main()
