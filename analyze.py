#!/usr/bin/env python3
"""
台股開盤前每日分析腳本
資料來源：Yahoo Finance (yfinance) + TWSE 官方 API
Claude 分析由 Remote Trigger 在 claude.ai 另外執行
"""
import requests
import yfinance as yf
from datetime import datetime, timedelta
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')
NOW    = datetime.now(TW_TZ)
TODAY  = NOW.strftime('%Y-%m-%d')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


# ── 匯率：Yahoo Finance ────────────────────────────────

def fetch_fx(ticker):
    """用 yfinance 取得匯率，回傳 (今日收盤, 前日收盤, 漲跌)"""
    try:
        data = yf.Ticker(ticker).history(period='5d')
        if data.empty or len(data) < 2:
            return None, None, None
        today_close = round(data['Close'].iloc[-1], 4)
        prev_close  = round(data['Close'].iloc[-2], 4)
        change      = round(today_close - prev_close, 4)
        return today_close, prev_close, change
    except Exception as e:
        print(f"Yahoo FX 取得失敗 {ticker}: {e}")
        return None, None, None


def fx_direction(change, threshold=0.1):
    """判斷升貶方向（USD/TWD 下跌 = 台幣升值）"""
    if change is None:
        return "資料未取得"
    # USD/TWD：數字跌 = 台幣升值
    if change < -threshold:
        return f"台幣升值 {abs(change):.3f}（明顯）"
    elif change > threshold:
        return f"台幣貶值 {change:.3f}（明顯）"
    else:
        return f"平盤（{change:+.3f}）"


# ── 加權指數 & 台指期：Yahoo Finance ──────────────────

def fetch_taiex():
    """TAIEX 加權指數收盤"""
    try:
        data = yf.Ticker('^TWII').history(period='5d')
        if data.empty:
            return None
        return round(data['Close'].iloc[-1], 2)
    except Exception as e:
        print(f"TAIEX 取得失敗: {e}")
        return None


def fetch_tx_futures():
    """台指期近月（TXF=F）收盤，Yahoo Finance 支援有限，失敗時回傳 None"""
    try:
        data = yf.Ticker('TXF=F').history(period='5d')
        if data.empty:
            return None
        return round(data['Close'].iloc[-1], 2)
    except Exception as e:
        print(f"台指期取得失敗: {e}")
        return None


# ── 三大法人：TWSE 官方 API ────────────────────────────

def fetch_institutional():
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        return r.json()
    except Exception as e:
        print(f"三大法人取得失敗: {e}")
        return None


def parse_institutional(data):
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


# ── 格式化工具 ─────────────────────────────────────────

def fmt_money(amount):
    try:
        val  = abs(int(amount)) / 1e8
        sign = '+' if int(amount) >= 0 else '-'
        return f"{sign}{val:.1f}億"
    except Exception:
        return "—"


# ── 主程式 ─────────────────────────────────────────────

def main():
    print(f"開始執行每日分析：{TODAY}\n")

    # ── 匯率（Yahoo Finance）──
    usd_today, usd_prev, usd_chg = fetch_fx('USDTWD=X')
    cny_today, cny_prev, cny_chg = fetch_fx('CNYTWD=X')
    krw_today, krw_prev, krw_chg = fetch_fx('KRWTWD=X')

    usd_str    = f"{usd_today}（前日 {usd_prev}）" if usd_today else "取得失敗"
    cny_str    = f"{cny_today}（前日 {cny_prev}）" if cny_today else "取得失敗"
    krw_str    = f"{krw_today}（前日 {krw_prev}）" if krw_today else "取得失敗"
    usd_judge  = fx_direction(usd_chg)

    # 三幣走向判斷（USD/TWD 跌 = 升值；CNY/TWD、KRW/TWD 跌 = 相對台幣升）
    currencies_up = sum([
        1 if usd_chg is not None and usd_chg < 0 else 0,   # 台幣升
        1 if cny_chg is not None and cny_chg < 0 else 0,   # 人民幣升（相對台幣）
        1 if krw_chg is not None and krw_chg < 0 else 0,   # 韓元升（相對台幣）
    ])
    if currencies_up == 3:
        three_currency = "三幣齊升 → 國際資金流入亞洲，偏多"
    elif currencies_up == 0:
        three_currency = "三幣齊貶 → 亞洲資金外流，偏空"
    elif usd_chg is not None and usd_chg < 0 and cny_chg is not None and cny_chg > 0:
        three_currency = "台幣升但人民幣/韓元貶 → 可能壽險拋匯，不持續"
    else:
        three_currency = "走向分歧，需人工判斷"

    # ── 加權指數 & 台指期（Yahoo Finance）──
    taiex_close = fetch_taiex()
    tx_close    = fetch_tx_futures()

    if tx_close and taiex_close:
        spread = tx_close - taiex_close
        spread_str   = f"{spread:+.0f} 點"
        spread_judge = (
            "正價差 > 100，偏多" if spread > 100 else
            "逆價差 > 100，偏空" if spread < -100 else
            "價差在 ±100 以內，中性"
        )
    else:
        spread       = None
        spread_str   = "取得失敗"
        spread_judge = "—"

    taiex_str = f"{taiex_close:,.2f}" if taiex_close else "取得失敗"
    tx_str    = f"{tx_close:,.2f}"    if tx_close    else "取得失敗（TXF=F 支援有限）"

    # ── 三大法人（TWSE）──
    inst    = parse_institutional(fetch_institutional())
    foreign = next((v for k, v in inst.items() if '外資' in k and '陸資' in k and '自行' not in k), None)
    trust   = next((v for k, v in inst.items() if '投信' in k), None)
    dealer  = next((v for k, v in inst.items() if '自營' in k and '自行' in k), None)
    total   = sum(v['diff'] for v in inst.values()) if inst else None

    foreign_str = fmt_money(foreign['diff']) if foreign else "取得失敗"
    trust_str   = fmt_money(trust['diff'])   if trust   else "取得失敗"
    dealer_str  = fmt_money(dealer['diff'])  if dealer  else "取得失敗"
    total_str   = fmt_money(total)           if total is not None else "取得失敗"

    # ── 結論 ──
    if foreign and foreign['diff'] > 0 and spread is not None and spread > 0:
        conclusion = f"方向偏多。外資買超 {foreign_str}，期現正價差 {spread_str}。{usd_judge}。"
    elif foreign and foreign['diff'] < 0:
        conclusion = f"外資賣超 {foreign_str}，方向偏空。今日謹慎，等方向明確再動作。"
    else:
        conclusion = f"訊號混雜，請至 claude.ai/code/scheduled 查看 Claude 完整分析。外資 {foreign_str}。"

    # ── 組合報告 ──
    report = f"""
## {TODAY}

### 匯率（Yahoo Finance）
| 幣別 | 今日 | 前日 | 判斷 |
|------|------|------|------|
| USD/TWD | {usd_str} | {usd_judge} |
| CNY/TWD | {cny_str} | — |
| KRW/TWD | {krw_str} | — |
| **三幣走向** | {three_currency} | |

### 台指期 & 現貨
| 項目 | 數字 |
|------|------|
| 加權指數（TAIEX） | {taiex_str} |
| 台指期（TXF） | {tx_str} |
| 期現價差 | {spread_str} |
| 判斷 | {spread_judge} |

### 三大法人（TWSE）
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
    print(f"已寫入 {obs_file}")

    # ── 寫入 summary.txt（ntfy 推播用）──
    usd_display    = f"USD/TWD {usd_today}（{usd_judge}）" if usd_today else "匯率未取得"
    spread_display = f"期現價差 {spread_str}" if spread is not None else "期現價差未取得"
    summary = (
        f"📅 {TODAY} 開盤前分析\n"
        f"💱 {usd_display}\n"
        f"🌏 {three_currency}\n"
        f"🏦 外資 {foreign_str} | 投信 {trust_str}\n"
        f"📊 {spread_display}\n"
        f"📝 {conclusion}"
    )
    with open('summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)
    print(f"已寫入 summary.txt")


if __name__ == '__main__':
    main()
