#!/usr/bin/env python3
"""
台股開盤前每日分析腳本
資料來源：台灣銀行、台灣證交所官方 API
結論由 Claude API 撰寫
"""
import os
import requests
import re
from datetime import datetime
import pytz
import anthropic

TW_TZ = pytz.timezone('Asia/Taipei')
TODAY = datetime.now(TW_TZ).strftime('%Y-%m-%d')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


# ── 匯率：台灣銀行 ─────────────────────────────────────

def fetch_bot_rate(currency):
    """台灣銀行即期匯率文字格式"""
    try:
        url = f"https://rate.bot.com.tw/xrt/fltxt/0/{currency}"
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.encoding = 'utf-8'
        return r.text.strip()
    except Exception as e:
        print(f"匯率取得失敗 {currency}: {e}")
        return None


def parse_bot_rate(text):
    """解析台灣銀行匯率，回傳 (即期買入, 即期賣出, 中間價)"""
    if not text:
        return None, None, None
    for line in text.split('\n'):
        parts = re.split(r'\s+', line.strip())
        if len(parts) >= 5:
            try:
                buy  = float(parts[3])
                sell = float(parts[4])
                mid  = round((buy + sell) / 2, 4)
                return buy, sell, mid
            except (ValueError, IndexError):
                continue
    return None, None, None


# ── 三大法人：台灣證交所 ───────────────────────────────

def fetch_institutional():
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        return r.json()
    except Exception as e:
        print(f"三大法人取得失敗: {e}")
        return None


def parse_institutional(data):
    """回傳 dict：{名稱: {'buy': int, 'sell': int, 'diff': int}}"""
    if not data:
        return {}
    result = {}
    for row in data.get('data', []):
        if len(row) >= 4:
            name = row[0].strip()
            try:
                result[name] = {
                    'buy':  int(row[1].replace(',', '')),
                    'sell': int(row[2].replace(',', '')),
                    'diff': int(row[3].replace(',', '')),
                }
            except (ValueError, IndexError):
                pass
    return result


# ── 台指期：期交所 ─────────────────────────────────────

def fetch_taifex_close():
    """期交所 TX 日盤最後成交價（用於計算期現價差）"""
    try:
        url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.encoding = 'utf-8'
        # 找 TX 近月合約的收盤欄位
        match = re.search(
            r'臺股期貨.*?TX.*?<td[^>]*>(\d[\d,]+)</td>\s*'
            r'<td[^>]*>(\d[\d,]+)</td>\s*'
            r'<td[^>]*>(\d[\d,]+)</td>\s*'
            r'<td[^>]*>(\d[\d,]+)</td>',
            r.text, re.DOTALL
        )
        if match:
            close = int(match.group(4).replace(',', ''))
            return close
        return None
    except Exception as e:
        print(f"期交所資料取得失敗: {e}")
        return None


def fetch_taiex_close():
    """加權指數收盤（TWSE）"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        data = r.json()
        for table in data.get('tables', []):
            title = table.get('title', '')
            if '加權' in title or '指數' in title:
                rows = table.get('data', [])
                if rows:
                    # 最後一列通常是加權指數
                    for row in reversed(rows):
                        try:
                            close = float(str(row[-2]).replace(',', ''))
                            return close
                        except (ValueError, IndexError):
                            continue
        return None
    except Exception as e:
        print(f"加權指數取得失敗: {e}")
        return None


# ── 格式化工具 ─────────────────────────────────────────

def fmt_money(amount):
    """元 → 億，帶正負號"""
    try:
        val = abs(int(amount)) / 1e8
        sign = '+' if int(amount) >= 0 else '-'
        return f"{sign}{val:.1f}億"
    except Exception:
        return "—"


def claude_conclusion(data_summary: str) -> str:
    """呼叫 Claude API，根據今日數據寫出開盤前結論"""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return "（未設定 ANTHROPIC_API_KEY，略過 AI 結論）"
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": f"""你是台股操盤手的開盤前助理。根據以下今日數據，用繁體中文寫出簡短結論（100字以內）。

格式固定：「方向偏OO。[關鍵理由1句]。觀察重點：XXX。條件才進。」

數據：
{data_summary}

注意：
- 升值超過0.1元才算明顯
- 三幣齊升才算外資真流入
- 外資買超搭配正價差才偏多
- 不要廢話，直接給結論"""}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"AI 結論取得失敗：{e}"


def main():
    print(f"開始執行每日分析：{TODAY}\n")

    # ── 匯率 ──
    usd_buy, usd_sell, usd_mid = parse_bot_rate(fetch_bot_rate('USD'))
    cny_buy, cny_sell, cny_mid = parse_bot_rate(fetch_bot_rate('CNY'))
    krw_buy, krw_sell, krw_mid = parse_bot_rate(fetch_bot_rate('KRW'))

    usd_str = f"買 {usd_buy} / 賣 {usd_sell}（中間 {usd_mid}）" if usd_buy else "取得失敗"
    cny_str = f"買 {cny_buy} / 賣 {cny_sell}（中間 {cny_mid}）" if cny_buy else "取得失敗"
    krw_str = f"買 {krw_buy} / 賣 {krw_sell}（中間 {krw_mid}）" if krw_buy else "取得失敗"

    # ── 三大法人 ──
    inst = parse_institutional(fetch_institutional())
    foreign = next((v for k, v in inst.items() if '外資' in k and '陸資' in k and '自行' not in k), None)
    trust   = next((v for k, v in inst.items() if '投信' in k), None)
    dealer  = next((v for k, v in inst.items() if '自營' in k and '自行' in k), None)
    total   = sum(v['diff'] for v in inst.values()) if inst else None

    foreign_str = fmt_money(foreign['diff']) if foreign else "取得失敗"
    trust_str   = fmt_money(trust['diff'])   if trust   else "取得失敗"
    dealer_str  = fmt_money(dealer['diff'])  if dealer  else "取得失敗"
    total_str   = fmt_money(total)           if total is not None else "取得失敗"

    # ── 台指期 / 現貨 ──
    tx_close    = fetch_taifex_close()
    taiex_close = fetch_taiex_close()

    if tx_close and taiex_close:
        spread = tx_close - taiex_close
        spread_str  = f"{spread:+.0f} 點"
        spread_judge = (
            "正價差 > 100，偏多" if spread > 100 else
            "逆價差 > 100，偏空" if spread < -100 else
            "價差在 ±100 以內，中性"
        )
    else:
        spread_str   = "取得失敗（請手動計算夜盤 - 現貨）"
        spread_judge = "—"

    tx_str    = str(tx_close)    if tx_close    else "取得失敗"
    taiex_str = str(taiex_close) if taiex_close else "取得失敗"

    # ── 今日結論（Claude API）──
    data_summary = f"""
USD/TWD 今日即期：買 {usd_buy} / 賣 {usd_sell}（需與前日16:00收盤比較）
CNY/TWD 今日即期：買 {cny_buy} / 賣 {cny_sell}
KRW/TWD 今日即期：買 {krw_buy} / 賣 {krw_sell}
台指期日盤收盤：{tx_close}
加權指數收盤：{taiex_close}
期現價差：{spread_str if tx_close and taiex_close else '無法計算'}
外資買超：{foreign_str}
投信買超：{trust_str}
自營商買超：{dealer_str}
三大法人合計：{total_str}
"""
    conclusion = claude_conclusion(data_summary)

    # ── 組合報告 ──
    report = f"""
## {TODAY}

### 匯率（台灣銀行即期，今日牌告）
| 幣別 | 今日報價 |
|------|---------|
| USD/TWD | {usd_str} |
| CNY/TWD | {cny_str} |
| KRW/TWD | {krw_str} |
| 三幣走向 | 請對照前日16:00收盤判斷（升超0.1元算明顯） |

### 台指期（日盤，供參考）
| 項目 | 數字 |
|------|------|
| TX 日盤收盤 | {tx_str} |
| 加權指數收盤 | {taiex_str} |
| 期現價差 | {spread_str} |
| 判斷 | {spread_judge} |
| 夜盤收盤 | 請手動查（期交所 / Yahoo 期貨）|

### 三大法人整體
| 法人 | 買超金額 |
|------|----------|
| 外資 | {foreign_str} |
| 投信 | {trust_str} |
| 自營商 | {dealer_str} |
| **合計** | **{total_str}** |

### 三大法人連續買超個股
（請手動查 [Goodinfo](https://goodinfo.tw/tw/StockList.asp?MARKET_CAT=%E6%99%BA%E6%85%A7%E9%81%B8%E8%82%A1&INDUSTRY_CAT=%E4%B8%89%E5%A4%A7%E6%B3%95%E4%BA%BA%E9%80%A3%E8%B2%B7+%E2%80%93+%E6%97%A5%40%40%E4%B8%89%E5%A4%A7%E6%B3%95%E4%BA%BA%E9%80%A3%E7%BA%8C%E8%B2%B7%E8%B6%85%40%40%E4%B8%89%E5%A4%A7%E6%B3%95%E4%BA%BA%E9%80%A3%E7%BA%8C%E8%B2%B7%E8%B6%85+%E2%80%93+%E6%97%A5) 或籌碼K線）

### 今日結論
{conclusion}

---"""

    print(report)

    # ── 寫入 observation.md ──
    obs_file = 'observation.md'
    try:
        with open(obs_file, 'r', encoding='utf-8') as f:
            existing = f.read()
    except FileNotFoundError:
        existing = '# 每日觀察紀錄\n\n---\n'

    with open(obs_file, 'w', encoding='utf-8') as f:
        f.write(existing.rstrip() + '\n' + report + '\n')

    print(f"\n已寫入 {obs_file}")


if __name__ == '__main__':
    main()
