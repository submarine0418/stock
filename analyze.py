#!/usr/bin/env python3
"""
台股開盤前每日分析腳本
資料來源：Yahoo Finance (yfinance) + TWSE 官方 API
AI 分析由 Claude Remote Trigger 另外執行

抓完數據後寫入 observation.md + summary.txt（供樹莓派推播）
"""
import json
import re
import requests
import yfinance as yf
from datetime import datetime, timedelta
import pytz

import os

TW_TZ = pytz.timezone('Asia/Taipei')
NOW = datetime.now(TW_TZ)
TODAY = NOW.strftime('%Y-%m-%d')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')
FINMIND_URL   = 'https://api.finmindtrade.com/api/v4/data'


# ── 匯率 ──────────────────────────────────────────────

def fetch_fx(ticker):
    try:
        data = yf.Ticker(ticker).history(period='5d')
        if not data.empty and len(data) >= 2:
            today_close = round(data['Close'].iloc[-1], 4)
            prev_close = round(data['Close'].iloc[-2], 4)
            change = round(today_close - prev_close, 4)
            print(f"  {ticker}: {today_close}")
            return today_close, prev_close, change
    except Exception as e:
        print(f"  Yahoo FX 失敗 {ticker}: {e}")
    return None, None, None


def fetch_usdtwd_bot():
    try:
        url = "https://rate.bot.com.tw/xrt/fltxt/0/USD"
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.encoding = 'utf-8'
        for line in r.text.split('\n'):
            parts = re.split(r'\s+', line.strip())
            if len(parts) >= 5:
                buy, sell = float(parts[3]), float(parts[4])
                mid = round((buy + sell) / 2, 4)
                print(f"  USD/TWD（台灣銀行）: {mid}")
                return mid, None, None
    except Exception as e:
        print(f"  台灣銀行匯率失敗: {e}")
    return None, None, None


def fx_direction(change, threshold=0.1):
    if change is None:
        return "資料未取得"
    if change < -threshold:
        return f"台幣升值 {abs(change):.3f}（明顯）"
    elif change > threshold:
        return f"台幣貶值 {change:.3f}（明顯）"
    else:
        return f"平盤（{change:+.3f}）"


# ── 指數 & 期貨 ───────────────────────────────────────

def fetch_taiex():
    try:
        data = yf.Ticker('^TWII').history(period='5d')
        if not data.empty:
            val = round(data['Close'].iloc[-1], 2)
            print(f"  TAIEX: {val}")
            return val
    except Exception as e:
        print(f"  TAIEX yfinance 失敗: {e}")
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        data = r.json()
        for table in data.get('tables', []):
            for row in table.get('data', []):
                if len(row) >= 2 and '加權' in str(row[0]):
                    val = float(str(row[1]).replace(',', ''))
                    print(f"  TAIEX（TWSE）: {val}")
                    return val
    except Exception as e:
        print(f"  TAIEX TWSE 失敗: {e}")
    return None


def fetch_tx_futures():
    try:
        data = yf.Ticker('TXF=F').history(period='5d')
        if not data.empty:
            val = round(data['Close'].iloc[-1], 2)
            print(f"  TX futures: {val}")
            return val
    except Exception as e:
        print(f"  台指期失敗: {e}")
    return None


# ── 三大法人（FinMind）────────────────────────────────

def fetch_institutional():
    """FinMind TaiwanStockTotalInstitutionalInvestors，fallback 到 TWSE BFI82U"""
    if FINMIND_TOKEN:
        try:
            r = requests.get(FINMIND_URL, params={
                'dataset':    'TaiwanStockTotalInstitutionalInvestors',
                'start_date': TODAY,
                'token':      FINMIND_TOKEN,
            }, timeout=15)
            rows = r.json().get('data', [])
            if rows:
                # FinMind 英文名稱 → 統一成中文 key
                name_map = {
                    'Foreign_Investor':    '外資及陸資(不含外資自營商)',
                    'Foreign_Dealer_Self': '外資自營商',
                    'Investment_Trust':    '投信',
                    'Dealer_self':         '自營商(自行買賣)',
                    'Dealer_Hedging':      '自營商(避險)',
                }
                result = {}
                for row in rows:
                    en_name = row['name']
                    zh_name = name_map.get(en_name, en_name)
                    buy = row.get('buy', 0)
                    sell = row.get('sell', 0)
                    result[zh_name] = {
                        'buy': buy,
                        'sell': sell,
                        'diff': buy - sell,
                    }
                print(f"  三大法人（FinMind）: {list(result.keys())}")
                return result
        except Exception as e:
            print(f"  FinMind 三大法人失敗: {e}")

    # fallback：TWSE BFI82U
    try:
        r = requests.get(
            'https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json',
            timeout=15, headers=HEADERS
        )
        data = r.json()
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
        print(f"  三大法人（TWSE fallback）: {list(result.keys())}")
        return result
    except Exception as e:
        print(f"  三大法人 TWSE 失敗: {e}")
        return {}


def parse_institutional(data):
    return data  # FinMind 版已是 dict，直接用


# ── 個股法人買超（FinMind）───────────────────────────

def fetch_top_stocks():
    """FinMind TaiwanStockInstitutionalInvestorsBuySell，fallback 到 TWSE T86"""
    if FINMIND_TOKEN:
        try:
            r = requests.get(FINMIND_URL, params={
                'dataset':    'TaiwanStockInstitutionalInvestorsBuySell',
                'start_date': TODAY,
                'token':      FINMIND_TOKEN,
            }, timeout=20)
            rows = r.json().get('data', [])
            if rows:
                # 依股票代號彙總三大法人
                stocks = {}
                for row in rows:
                    sid  = row['stock_id']
                    name = row.get('stock_name', sid)
                    net  = row.get('buy', 0) - row.get('sell', 0)
                    inst = row.get('name', '')
                    if sid not in stocks:
                        stocks[sid] = {'code': sid, 'name': name,
                                       'total': 0, 'foreign': 0, 'trust': 0}
                    stocks[sid]['total'] += net
                    if 'Foreign' in inst and 'Dealer' not in inst:
                        stocks[sid]['foreign'] += net
                    elif 'Investment_Trust' in inst:
                        stocks[sid]['trust']   += net

                results = [v for v in stocks.values() if v['total'] > 0]
                results.sort(key=lambda x: x['total'], reverse=True)
                print(f"  個股法人（FinMind）: {len(results)} 檔買超")
                return results[:15]
        except Exception as e:
            print(f"  FinMind 個股失敗: {e}")

    # fallback：TWSE T86
    try:
        r = requests.get(
            'https://www.twse.com.tw/rwd/zh/fund/T86?selectType=ALL&response=json',
            timeout=15, headers=HEADERS
        )
        try:
            data = json.loads(r.content.decode('utf-8'))
        except Exception:
            data = json.loads(r.content.decode('big5', errors='replace'))

        def to_int(s):
            try:
                return int(str(s).replace(',', '').replace('+', '').strip())
            except ValueError:
                return 0

        results = []
        for row in data.get('data', []):
            if len(row) < 19:
                continue
            total = to_int(row[18])
            if total <= 0:
                continue
            results.append({
                'code':    row[0].strip(),
                'name':    row[1].strip(),
                'total':   total,
                'foreign': to_int(row[4]),
                'trust':   to_int(row[10]),
            })
        results.sort(key=lambda x: x['total'], reverse=True)
        print(f"  個股法人（TWSE fallback）: {len(results)} 檔買超")
        return results[:15]
    except Exception as e:
        print(f"  T86 fallback 失敗: {e}")
        return []


# ── 美股指數 ─────────────────────────────────────────

def fetch_us_market():
    indices = {
        '^GSPC': 'S&P 500',
        '^IXIC': 'NASDAQ',
        '^DJI': '道瓊',
        '^SOX': '費半',
        '^VIX': 'VIX',
    }
    results = {}
    for ticker, name in indices.items():
        try:
            data = yf.Ticker(ticker).history(period='5d')
            if not data.empty and len(data) >= 2:
                close = round(data['Close'].iloc[-1], 2)
                prev = round(data['Close'].iloc[-2], 2)
                chg = round(close - prev, 2)
                pct = round((chg / prev) * 100, 2)
                results[name] = {'close': close, 'change': chg, 'pct': pct}
        except Exception:
            pass
    return results


# ── 格式化 ────────────────────────────────────────────

def fmt_money(amount):
    try:
        val = abs(int(amount)) / 1e8
        sign = '+' if int(amount) >= 0 else '-'
        return f"{sign}{val:.1f}億"
    except Exception:
        return "—"


# ── 主程式 ────────────────────────────────────────────

def main():
    print(f"=== 每日台股分析 {TODAY} ===\n")

    # 匯率
    print("[1/5] 匯率...")
    usd_today, usd_prev, usd_chg = fetch_fx('USDTWD=X')
    if not usd_today:
        usd_today, usd_prev, usd_chg = fetch_usdtwd_bot()
    cny_today, cny_prev, cny_chg = fetch_fx('CNYTWD=X')
    krw_today, krw_prev, krw_chg = fetch_fx('KRWTWD=X')
    usd_judge = fx_direction(usd_chg)

    # 三幣判斷
    currencies_up = sum([
        1 if usd_chg is not None and usd_chg < 0 else 0,
        1 if cny_chg is not None and cny_chg < 0 else 0,
        1 if krw_chg is not None and krw_chg < 0 else 0,
    ])
    if currencies_up == 3:
        three_currency = "三幣齊升 → 國際資金流入亞洲，偏多"
    elif currencies_up == 0:
        three_currency = "三幣齊貶 → 亞洲資金外流，偏空"
    elif usd_chg is not None and usd_chg < 0 and currencies_up == 1:
        three_currency = "僅台幣升，可能壽險拋匯，不持續"
    else:
        three_currency = "走向分歧，需人工判斷"

    # 指數 & 期貨
    print("[2/5] 指數 & 期貨...")
    taiex = fetch_taiex()
    tx = fetch_tx_futures()
    spread = (tx - taiex) if (tx and taiex) else None

    if spread is not None:
        spread_str = f"{spread:+.0f} 點"
        if spread > 100:
            spread_judge = "正價差 > 100，偏多"
        elif spread < -100:
            spread_judge = "逆價差 > 100，偏空"
        else:
            spread_judge = "±100 以內，中性"
    else:
        spread_str = "取得失敗"
        spread_judge = "—"

    # 法人
    print("[3/5] 三大法人...")
    inst = parse_institutional(fetch_institutional())
    foreign = next((v for k, v in inst.items() if '外資' in k and '陸資' in k and '自行' not in k), None)
    trust = next((v for k, v in inst.items() if '投信' in k), None)
    dealer = next((v for k, v in inst.items() if '自營' in k and '自行' in k), None)

    foreign_str = fmt_money(foreign['diff']) if foreign else "取得失敗"
    trust_str = fmt_money(trust['diff']) if trust else "取得失敗"
    dealer_str = fmt_money(dealer['diff']) if dealer else "取得失敗"

    # 買超個股
    print("[4/5] 法人買超個股...")
    top_stocks = fetch_top_stocks()
    if top_stocks:
        stock_rows = '\n'.join(
            f"| {s['code']} | {s['name']} | {s['total']:+,} | {s['foreign']:+,} | {s['trust']:+,} |"
            for s in top_stocks
        )
        stock_table = f"""| 代號 | 名稱 | 三大合計 | 外資 | 投信 |
|------|------|---------|------|------|
{stock_rows}"""
    else:
        stock_table = "資料未取得"

    # 美股
    print("[5/5] 美股...")
    us_market = fetch_us_market()
    if us_market:
        us_rows = '\n'.join(
            f"| {name} | {d['close']:,.2f} | {d['change']:+,.2f} | {d['pct']:+.2f}% |"
            for name, d in us_market.items()
        )
        us_table = f"""| 指數 | 收盤 | 漲跌 | 漲跌% |
|------|------|------|-------|
{us_rows}"""
    else:
        us_table = "取得失敗"

    # 結論（簡易自動判斷，AI 分析由 Claude Remote Trigger 補充）
    signals = []
    if foreign and foreign['diff'] > 0:
        signals.append(f"外資買超 {foreign_str}")
    elif foreign and foreign['diff'] < 0:
        signals.append(f"外資賣超 {foreign_str}")
    if spread is not None and spread > 100:
        signals.append(f"期現正價差 {spread_str}")
    elif spread is not None and spread < -100:
        signals.append(f"期現逆價差 {spread_str}")
    if usd_chg is not None and usd_chg < -0.1:
        signals.append("台幣明顯升值")
    elif usd_chg is not None and usd_chg > 0.1:
        signals.append("台幣明顯貶值")

    bullish = sum(1 for s in signals if any(w in s for w in ['買超', '正價差', '升值']))
    bearish = sum(1 for s in signals if any(w in s for w in ['賣超', '逆價差', '貶值']))

    if bullish > bearish:
        direction = "偏多"
    elif bearish > bullish:
        direction = "偏空"
    else:
        direction = "中性"

    conclusion = f"方向{direction}。{'、'.join(signals) if signals else '訊號不足'}。"

    # ── 組合報告 ──
    report = f"""
## {TODAY}

### 昨晚美股
{us_table}

### 匯率
| 幣別 | 今日 | 前日 | 判斷 |
|------|------|------|------|
| USD/TWD | {usd_today or '—'} | {usd_prev or '—'} | {usd_judge} |
| CNY/TWD | {cny_today or '—'} | {cny_prev or '—'} | — |
| KRW/TWD | {krw_today or '—'} | {krw_prev or '—'} | — |

三幣走向：{three_currency}

### 台指期 & 現貨
| 項目 | 數字 |
|------|------|
| 加權指數 | {taiex:,.2f if taiex else '取得失敗'} |
| 台指期 | {tx:,.2f if tx else '取得失敗'} |
| 期現價差 | {spread_str} |
| 判斷 | {spread_judge} |

### 三大法人
| 法人 | 買超金額 |
|------|----------|
| 外資 | {foreign_str} |
| 投信 | {trust_str} |
| 自營商 | {dealer_str} |

### 法人買超個股（前15）
{stock_table}

### 今日結論（自動判斷）
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
    print(f"\n✅ 已寫入 {obs_file}")

    # ── 寫入 summary.txt（推播用）──
    if top_stocks:
        top5 = '\n'.join(
            f"  {s['code']} {s['name']} 外{fmt_money(s['foreign'])} 投{fmt_money(s['trust'])}"
            for s in top_stocks[:5]
        )
        top5_str = f"\n📈 買超前5：\n{top5}"
    else:
        top5_str = ""

    us_brief = ""
    if us_market.get('S&P 500'):
        sp = us_market['S&P 500']
        us_brief = f"\n🇺🇸 S&P500 {sp['close']:,.0f}（{sp['pct']:+.2f}%）"
    if us_market.get('費半'):
        sox = us_market['費半']
        us_brief += f" | 費半 {sox['pct']:+.2f}%"

    summary = (
        f"📅 {TODAY} 台股數據{us_brief}\n"
        f"💱 USD/TWD {usd_today or '?'}（{usd_judge}）\n"
        f"🌏 {three_currency}\n"
        f"🏦 外資 {foreign_str} | 投信 {trust_str}\n"
        f"📊 期現價差 {spread_str}（{spread_judge}）\n"
        f"📝 {conclusion}"
        f"{top5_str}"
    )

    with open('summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)
    print(f"✅ 已寫入 summary.txt")


if __name__ == '__main__':
    main()
