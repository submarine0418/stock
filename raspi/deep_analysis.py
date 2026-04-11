#!/usr/bin/env python3
"""
樹莓派深度分析腳本
每天在 notify.py 推播後執行，針對買超榜上的個股做基本面分析：
1. 從 observation.md 抓出當天買超個股
2. 用 FinMind API 查每檔的：月營收、PER/PBR、殖利率、近期股價
3. 篩選出「法人買 + 基本面好 + 位置甜」的標的
4. 推送深度分析到 Telegram

用法：
  python deep_analysis.py          # 分析今天的買超個股
  python deep_analysis.py --test   # 測試用，分析 2330 台積電
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / 'config.json'


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    print("❌ config.json 不存在，請先執行 notify.py")
    sys.exit(1)


def finmind_get(dataset, params, token):
    """呼叫 FinMind API"""
    base = 'https://api.finmindtrade.com/api/v4/data'
    params['dataset'] = dataset
    qs = urllib.parse.urlencode(params)
    url = f"{base}?{qs}"
    try:
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if data.get('msg') == 'success':
                return data.get('data', [])
            else:
                print(f"  FinMind {dataset}: {data.get('msg', 'unknown error')}")
                return []
    except Exception as e:
        print(f"  FinMind {dataset} 失敗: {e}")
        return []


def extract_stock_codes(repo_path):
    """從 observation.md 最新一筆分析中提取買超個股代號"""
    obs_file = os.path.join(repo_path, 'observation.md')
    if not os.path.exists(obs_file):
        return []

    with open(obs_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 找最新一筆
    sections = re.split(r'(?=## \d{4}-\d{2}-\d{2})', content)
    sections = [s.strip() for s in sections if s.strip() and s.strip().startswith('## ')]
    if not sections:
        return []

    latest = sections[-1]

    # 提取股票代號（4位數字，排除 ETF 00 開頭）
    codes = re.findall(r'\b(\d{4})\b', latest)
    # 去重，保持順序
    seen = set()
    unique = []
    for c in codes:
        if c not in seen and not c.startswith('00'):
            seen.add(c)
            unique.append(c)
    return unique[:10]  # 最多分析10檔


def get_per_pbr(stock_id, token):
    """取得最新 PER、PBR、殖利率"""
    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    rows = finmind_get('TaiwanStockPER', {
        'data_id': stock_id, 'start_date': start,
    }, token)
    if rows:
        latest = rows[-1]
        return {
            'PER': latest.get('PER', 0),
            'PBR': latest.get('PBR', 0),
            'dividend_yield': latest.get('dividend_yield', 0),
        }
    return None


def get_monthly_revenue(stock_id, token):
    """取得近12個月營收，計算 YoY 成長"""
    start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
    rows = finmind_get('TaiwanStockMonthRevenue', {
        'data_id': stock_id, 'start_date': start,
    }, token)
    if not rows or len(rows) < 2:
        return None

    # 最新月營收
    latest = rows[-1]
    latest_rev = latest.get('revenue', 0)
    latest_month = latest.get('revenue_month', 0)
    latest_year = latest.get('revenue_year', 0)

    # 找去年同月
    yoy = None
    for r in rows:
        if r.get('revenue_year') == latest_year - 1 and r.get('revenue_month') == latest_month:
            prev_rev = r.get('revenue', 0)
            if prev_rev > 0:
                yoy = round(((latest_rev - prev_rev) / prev_rev) * 100, 1)
            break

    # 近3個月趨勢
    recent_3 = rows[-3:] if len(rows) >= 3 else rows
    revs = [r.get('revenue', 0) for r in recent_3]
    if len(revs) >= 2:
        if revs[-1] > revs[-2] > revs[0] if len(revs) >= 3 else revs[-1] > revs[-2]:
            trend = "連續成長 📈"
        elif revs[-1] < revs[-2]:
            trend = "衰退 📉"
        else:
            trend = "持平"
    else:
        trend = "資料不足"

    return {
        'latest_rev': latest_rev,
        'latest_month': f"{latest_year}/{latest_month}",
        'yoy': yoy,
        'trend': trend,
    }


def get_stock_price(stock_id, token):
    """取得近期股價，計算月線位置"""
    start = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    rows = finmind_get('TaiwanStockPrice', {
        'data_id': stock_id, 'start_date': start,
    }, token)
    if not rows:
        return None

    closes = [r.get('close', 0) for r in rows if r.get('close', 0) > 0]
    if not closes:
        return None

    price = closes[-1]
    ma20 = round(sum(closes[-20:]) / min(len(closes), 20), 2) if len(closes) >= 5 else None

    # 近5日漲跌
    if len(closes) >= 5:
        chg_5d = round(((closes[-1] - closes[-5]) / closes[-5]) * 100, 1)
    else:
        chg_5d = None

    position = None
    if ma20:
        diff_pct = ((price - ma20) / ma20) * 100
        if diff_pct < -20:
            position = "低檔（月線下20%+）→ 若法人連買，可能在摸底"
        elif diff_pct > 20:
            position = "高檔（月線上20%+）→ 小心出貨"
        elif -5 <= diff_pct <= 5:
            position = "盤整區（月線±5%）→ 最甜位置，等突破"
        elif diff_pct > 5:
            position = f"月線上{diff_pct:.0f}% → 多頭排列"
        else:
            position = f"月線下{abs(diff_pct):.0f}% → 偏弱"

    return {
        'price': price,
        'ma20': ma20,
        'chg_5d': chg_5d,
        'position': position,
    }


def get_stock_name(stock_id, token):
    """取得股票名稱"""
    rows = finmind_get('TaiwanStockInfo', {}, token)
    for r in rows:
        if r.get('stock_id') == stock_id:
            return r.get('stock_name', stock_id)
    return stock_id


def analyze_stock(stock_id, token):
    """綜合分析一檔股票"""
    print(f"  分析 {stock_id}...")

    per_data = get_per_pbr(stock_id, token)
    rev_data = get_monthly_revenue(stock_id, token)
    price_data = get_stock_price(stock_id, token)

    lines = [f"📌 {stock_id}"]

    # 股價
    if price_data:
        lines.append(f"  股價 {price_data['price']}")
        if price_data['ma20']:
            lines.append(f"  月線(20MA) {price_data['ma20']}")
        if price_data['position']:
            lines.append(f"  位置：{price_data['position']}")
        if price_data['chg_5d'] is not None:
            lines.append(f"  近5日 {price_data['chg_5d']:+.1f}%")

    # PER/PBR
    if per_data:
        per = per_data['PER']
        pbr = per_data['PBR']
        dy = per_data['dividend_yield']
        lines.append(f"  PER {per} | PBR {pbr} | 殖利率 {dy}%")
        if per > 0:
            if per < 10:
                lines.append("  → 本益比偏低，可能被低估")
            elif per > 30:
                lines.append("  → 本益比偏高，注意是否有成長支撐")

    # 營收
    if rev_data:
        rev_b = rev_data['latest_rev'] / 1e8
        lines.append(f"  最新營收（{rev_data['latest_month']}）{rev_b:.1f}億")
        if rev_data['yoy'] is not None:
            yoy = rev_data['yoy']
            if yoy > 20:
                lines.append(f"  YoY {yoy:+.1f}% 🔥 高成長")
            elif yoy > 0:
                lines.append(f"  YoY {yoy:+.1f}% 穩定成長")
            else:
                lines.append(f"  YoY {yoy:+.1f}% ⚠ 衰退")
        lines.append(f"  趨勢：{rev_data['trend']}")

    # 綜合評分
    score = 0
    reasons = []

    if price_data and price_data['position'] and '盤整' in price_data['position']:
        score += 2
        reasons.append("盤整區位置佳")
    elif price_data and price_data['position'] and '低檔' in price_data['position']:
        score += 1
        reasons.append("低檔可能摸底")
    elif price_data and price_data['position'] and '高檔' in price_data['position']:
        score -= 1
        reasons.append("高檔注意出貨")

    if rev_data and rev_data['yoy'] is not None and rev_data['yoy'] > 10:
        score += 1
        reasons.append("營收成長")
    elif rev_data and rev_data['yoy'] is not None and rev_data['yoy'] < -10:
        score -= 1
        reasons.append("營收衰退")

    if per_data and 0 < per_data['PER'] < 15:
        score += 1
        reasons.append("本益比合理")

    if per_data and per_data['dividend_yield'] > 4:
        score += 1
        reasons.append(f"高殖利率{per_data['dividend_yield']}%")

    if score >= 2:
        verdict = f"⭐ 值得關注（{', '.join(reasons)}）"
    elif score >= 1:
        verdict = f"👀 可觀察（{', '.join(reasons)}）"
    elif score <= -1:
        verdict = f"⚠ 謹慎（{', '.join(reasons)}）"
    else:
        verdict = "➡ 中性"

    lines.append(f"  {verdict}")

    return '\n'.join(lines), score


def telegram_send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': chat_id, 'text': text,
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get('ok', False)
    except Exception as e:
        print(f"  Telegram 失敗: {e}")
        return False


def main():
    cfg = load_config()
    finmind_token = cfg.get('finmind_token', '')

    if not finmind_token:
        print("❌ config.json 缺少 finmind_token")
        print("   去 https://finmind.github.io/ 註冊免費帳號拿 token")
        print("   然後加到 config.json：\"finmind_token\": \"你的token\"")
        sys.exit(1)

    repo_path = cfg.get('repo_path', '')

    # --test 模式
    if '--test' in sys.argv:
        codes = ['2330']
        print("測試模式：分析台積電 2330")
    else:
        codes = extract_stock_codes(repo_path)
        if not codes:
            print("今天沒有買超個股可分析")
            return

    print(f"=== 深度分析 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"分析 {len(codes)} 檔：{', '.join(codes)}\n")

    results = []
    for code in codes:
        analysis, score = analyze_stock(code, finmind_token)
        results.append((analysis, score))
        print(analysis)
        print()

    # 按評分排序
    results.sort(key=lambda x: x[1], reverse=True)

    # 組合推播訊息：全部都列完整分析
    header = f"🔍 深度分析 {datetime.now().strftime('%m/%d')}\n"
    header += f"分析了 {len(codes)} 檔法人買超個股\n\n"

    all_analyses = '\n\n'.join(r[0] for r in results)
    msg = header + all_analyses
    msg += "\n\n⚠ 以上僅供參考，進場前請確認站上月線"

    # Telegram 推送
    if cfg.get('telegram_bot_token') and cfg.get('telegram_chat_id'):
        print("\n推送到 Telegram...")
        # Telegram 限制 4096 字
        if len(msg) > 4000:
            msg = msg[:3900] + "\n\n...（太長被截斷）"
        ok = telegram_send(cfg['telegram_bot_token'], cfg['telegram_chat_id'], msg)
        if ok:
            print("✅ 推送成功")
        else:
            print("❌ 推送失敗")

    # 也寫入檔案
    output_file = os.path.join(repo_path, 'deep_analysis.txt') if repo_path else 'deep_analysis.txt'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(msg)
    print(f"✅ 已寫入 {output_file}")


if __name__ == '__main__':
    main()
