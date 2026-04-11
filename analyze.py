#!/usr/bin/env python3
"""
台股開盤前每日分析腳本
嚴格按照 CLAUDE.md 的三件事邏輯產出報告：
  第一件事：匯率判讀（升貶方向 + 三幣對照 + 央行防線）
  第二件事：期貨夜盤（期現價差 + 美股連動異常判斷）
  第三件事：法人籌碼（三大法人 + 個股買超 + 買超金額判斷）

資料來源：FinMind API（優先）→ Yahoo Finance → TWSE（備援）
"""
import json
import os
import re
import requests
import yfinance as yf
from datetime import datetime, timedelta
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')
NOW = datetime.now(TW_TZ)
TODAY = NOW.strftime('%Y-%m-%d')
MONTH = NOW.month
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')
FINMIND_URL = 'https://api.finmindtrade.com/api/v4/data'


# ═══════════════════════════════════════════════════════
#  資料抓取
# ═══════════════════════════════════════════════════════

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


def fetch_institutional():
    """FinMind TaiwanStockTotalInstitutionalInvestors → TWSE BFI82U"""
    if FINMIND_TOKEN:
        try:
            r = requests.get(FINMIND_URL, params={
                'dataset': 'TaiwanStockTotalInstitutionalInvestors',
                'start_date': TODAY,
                'token': FINMIND_TOKEN,
            }, timeout=15)
            rows = r.json().get('data', [])
            if rows:
                name_map = {
                    'Foreign_Investor':    '外資及陸資(不含外資自營商)',
                    'Foreign_Dealer_Self': '外資自營商',
                    'Investment_Trust':    '投信',
                    'Dealer_self':         '自營商(自行買賣)',
                    'Dealer_Hedging':      '自營商(避險)',
                }
                result = {}
                for row in rows:
                    zh = name_map.get(row['name'], row['name'])
                    buy, sell = row.get('buy', 0), row.get('sell', 0)
                    result[zh] = {'buy': buy, 'sell': sell, 'diff': buy - sell}
                print(f"  三大法人（FinMind）: OK")
                return result
        except Exception as e:
            print(f"  FinMind 三大法人失敗: {e}")
    try:
        r = requests.get('https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json',
                         timeout=15, headers=HEADERS)
        data = r.json()
        result = {}
        for row in data.get('data', []):
            if len(row) >= 4:
                name = row[0].strip()
                try:
                    result[name] = {
                        'buy': int(row[1].replace(',', '')),
                        'sell': int(row[2].replace(',', '')),
                        'diff': int(row[3].replace(',', '')),
                    }
                except (ValueError, IndexError):
                    pass
        print(f"  三大法人（TWSE fallback）: OK")
        return result
    except Exception as e:
        print(f"  三大法人 TWSE 失敗: {e}")
        return {}


def fetch_top_stocks():
    """FinMind 個股法人買超 → TWSE T86"""
    if FINMIND_TOKEN:
        try:
            r = requests.get(FINMIND_URL, params={
                'dataset': 'TaiwanStockInstitutionalInvestorsBuySell',
                'start_date': TODAY,
                'token': FINMIND_TOKEN,
            }, timeout=20)
            rows = r.json().get('data', [])
            if rows:
                stocks = {}
                for row in rows:
                    sid = row['stock_id']
                    name = row.get('stock_name', sid)
                    net = row.get('buy', 0) - row.get('sell', 0)
                    inst = row.get('name', '')
                    if sid not in stocks:
                        stocks[sid] = {'code': sid, 'name': name,
                                       'total': 0, 'foreign': 0, 'trust': 0}
                    stocks[sid]['total'] += net
                    if 'Foreign' in inst and 'Dealer' not in inst:
                        stocks[sid]['foreign'] += net
                    elif 'Investment_Trust' in inst:
                        stocks[sid]['trust'] += net
                results = [v for v in stocks.values() if v['total'] > 0]
                results.sort(key=lambda x: x['total'], reverse=True)
                print(f"  個股法人（FinMind）: {len(results)} 檔買超")
                return results[:15]
        except Exception as e:
            print(f"  FinMind 個股失敗: {e}")
    try:
        r = requests.get('https://www.twse.com.tw/rwd/zh/fund/T86?selectType=ALL&response=json',
                         timeout=15, headers=HEADERS)
        try:
            data = json.loads(r.content.decode('utf-8'))
        except Exception:
            data = json.loads(r.content.decode('big5', errors='replace'))
        def to_int(s):
            try: return int(str(s).replace(',', '').replace('+', '').strip())
            except ValueError: return 0
        results = []
        for row in data.get('data', []):
            if len(row) < 19: continue
            total = to_int(row[18])
            if total <= 0: continue
            results.append({
                'code': row[0].strip(), 'name': row[1].strip(),
                'total': total, 'foreign': to_int(row[4]), 'trust': to_int(row[10]),
            })
        results.sort(key=lambda x: x['total'], reverse=True)
        print(f"  個股法人（TWSE fallback）: {len(results)} 檔買超")
        return results[:15]
    except Exception as e:
        print(f"  T86 fallback 失敗: {e}")
        return []


def fetch_stock_price(stock_id):
    """用 yfinance 抓個股近期收盤價，計算月線（20MA）"""
    try:
        ticker = f"{stock_id}.TW"
        data = yf.Ticker(ticker).history(period='2mo')
        if not data.empty and len(data) >= 20:
            close = round(data['Close'].iloc[-1], 2)
            ma20 = round(data['Close'].tail(20).mean(), 2)
            return close, ma20
        elif not data.empty:
            close = round(data['Close'].iloc[-1], 2)
            return close, None
    except Exception:
        pass
    return None, None


def fetch_us_market():
    indices = {
        '^GSPC': 'S&P 500', '^IXIC': 'NASDAQ',
        '^DJI': '道瓊', '^SOX': '費半', '^VIX': 'VIX',
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


def fmt_money(amount):
    try:
        val = abs(int(amount)) / 1e8
        sign = '+' if int(amount) >= 0 else '-'
        return f"{sign}{val:.1f}億"
    except Exception:
        return "—"


# ═══════════════════════════════════════════════════════
#  分析邏輯（按照 CLAUDE.md 三件事）
# ═══════════════════════════════════════════════════════

def analyze_fx(usd_today, usd_prev, usd_chg, cny_today, cny_prev, cny_chg,
               krw_today, krw_prev, krw_chg):
    """第一件事：匯率判讀"""
    lines = []

    # 1. 升貶方向
    if usd_chg is None:
        lines.append("⚠ USD/TWD 資料未取得，無法判斷匯率方向")
        return '\n'.join(lines), "未知", []

    if usd_chg < -0.1:
        direction = "升值"
        lines.append(f"✅ 台幣明顯升值 {abs(usd_chg):.3f} 元（超過1角）→ 外資錢進來，權值股有機會")
    elif usd_chg > 0.1:
        direction = "貶值"
        lines.append(f"⚠ 台幣明顯貶值 {usd_chg:.3f} 元 → 外資匯出，今天別衝")
    else:
        direction = "平盤"
        lines.append(f"➡ 台幣平盤（{usd_chg:+.3f}）→ 回到個股籌碼判斷")

    # 2. 三幣對照（台幣、人民幣、韓元）
    twd_up = usd_chg < 0  # USD/TWD 跌 = 台幣升
    cny_up = cny_chg is not None and cny_chg < 0
    krw_up = krw_chg is not None and krw_chg < 0
    ups = sum([twd_up, cny_up, krw_up])

    signals = []
    if ups == 3:
        lines.append("🌏 三幣齊升 → 國際資金真的在流入亞洲，大盤安全，甚至可以加碼")
        signals.append("三幣齊升")
    elif ups == 0:
        lines.append("🌏 三幣齊貶 → 亞洲資金外流，偏空")
        signals.append("三幣齊貶")
    elif twd_up and not cny_up and not krw_up:
        lines.append("🌏 只有台幣在升 → 可能是壽險或出口商拋匯，買盤不持續，別追高")
        signals.append("僅台幣升")
    elif not twd_up and not cny_up and krw_up:
        lines.append("🌏 台幣貶、人民幣也貶，但韓元在升 → 小心！外資在賣台股買韓股，台積電可能被倒貨")
        signals.append("韓元獨升")
    else:
        lines.append(f"🌏 三幣走向分歧（{ups}/3 升值），需搭配其他指標判斷")

    # 3. 央行防線（32元關卡）
    if usd_today:
        if 31.9 <= usd_today <= 32.1:
            lines.append(f"🏛 USD/TWD {usd_today} 接近32元整數關卡 → 注意央行是否防守")
        elif usd_today < 31.9:
            lines.append(f"🏛 USD/TWD {usd_today} 已遠低於32元 → 央行未防守，外資主導")
        else:
            lines.append(f"🏛 USD/TWD {usd_today} 在32元以上")

    return '\n'.join(lines), direction, signals


def analyze_futures(taiex, tx, spread, us_market):
    """第二件事：期貨夜盤"""
    lines = []

    if spread is not None:
        # 基本判斷
        if spread > 100:
            lines.append(f"📈 期現正價差 {spread:+.0f} 點（> 100）→ 開高機率高")
        elif spread < -100:
            # 除息旺季例外
            if 6 <= MONTH <= 8:
                lines.append(f"📉 期現逆價差 {spread:+.0f} 點，但現在是 {MONTH} 月除息旺季")
                lines.append("   → 台指期本來就會逆價差100-300點，要扣掉除息點數再判斷")
                lines.append("   → 不一定是看空，是正常現象")
            else:
                lines.append(f"📉 期現逆價差 {spread:+.0f} 點（> 100）→ 開低機率高")
        else:
            lines.append(f"➡ 期現價差 {spread:+.0f} 點（±100 以內）→ 中性")

        # 美股連動異常判斷
        sp = us_market.get('S&P 500')
        if sp and sp['pct'] > 1.0 and spread < 50:
            lines.append(f"⚠ 美股大漲（S&P +{sp['pct']:.1f}%），但台指期夜盤沒怎麼動 → 台股相對弱勢，不建議追高")
        elif sp and sp['pct'] < -1.0 and spread > -50:
            lines.append(f"💡 美股大跌（S&P {sp['pct']:.1f}%），但台指期沒怎麼跌 → 台股相對抗跌")
    else:
        lines.append("⚠ 台指期資料取得失敗（Yahoo Finance TXF=F 支援有限）")

    return '\n'.join(lines)


def analyze_chips(inst, foreign, trust, dealer, top_stocks):
    """第三件事：法人籌碼"""
    lines = []

    # 三大法人整體
    if foreign:
        f_diff = foreign['diff']
        if f_diff > 0:
            lines.append(f"🟢 外資買超 {fmt_money(f_diff)}")
        else:
            lines.append(f"🔴 外資賣超 {fmt_money(f_diff)}")
    if trust:
        t_diff = trust['diff']
        if t_diff > 0:
            lines.append(f"🟢 投信買超 {fmt_money(t_diff)}")
        else:
            lines.append(f"🔴 投信賣超 {fmt_money(t_diff)}")
    if dealer:
        d_diff = dealer['diff']
        if d_diff > 0:
            lines.append(f"🟢 自營商買超 {fmt_money(d_diff)}")
        else:
            lines.append(f"🔴 自營商賣超 {fmt_money(d_diff)}")

    # 三方齊買/齊賣判斷
    if foreign and trust and dealer:
        all_buy = foreign['diff'] > 0 and trust['diff'] > 0 and dealer['diff'] > 0
        all_sell = foreign['diff'] < 0 and trust['diff'] < 0 and dealer['diff'] < 0
        if all_buy:
            lines.append("🔥 三大法人齊買，籌碼面非常強勁")
        elif all_sell:
            lines.append("❄ 三大法人齊賣，籌碼面非常弱")

    # 外資買超方向 vs 匯率一致性（由 main 補充）

    return '\n'.join(lines)


def analyze_stock_detail(top_stocks):
    """個股買超分析：按照 CLAUDE.md 邏輯看買超金額 + 股價位置"""
    lines = []
    watchlist = []

    if not top_stocks:
        return "資料未取得", []

    # 排除 ETF，只看個股
    individual = [s for s in top_stocks if not s['code'].startswith('00')]
    etfs = [s for s in top_stocks if s['code'].startswith('00')]

    if individual:
        lines.append("個股：")
        for s in individual[:8]:
            code, name = s['code'], s['name']
            total, foreign_net, trust_net = s['total'], s['foreign'], s['trust']

            # 嘗試抓股價和月線
            price, ma20 = fetch_stock_price(code)

            detail = f"  {code} {name} | 三大 {total:+,} | 外資 {foreign_net:+,} | 投信 {trust_net:+,}"

            if price and ma20:
                diff_pct = ((price - ma20) / ma20) * 100
                if diff_pct < -20:
                    position = "低檔（月線下20%+）"
                    note = "→ 若分點連買3天，主力可能在摸底"
                elif diff_pct > 20:
                    position = "高檔（月線上20%+）"
                    note = "→ 小心最後出貨，要看誰在賣"
                elif -5 <= diff_pct <= 5:
                    position = "盤整區（月線±5%）"
                    note = "→ 最甜位置，主力可能在吸籌"
                else:
                    position = f"月線{'上' if diff_pct > 0 else '下'}{abs(diff_pct):.0f}%"
                    note = ""
                detail += f"\n    股價 {price} | 月線(20MA) {ma20} | {position}"
                if note:
                    detail += f"\n    {note}"

                # 加入觀察名單的條件
                if -5 <= diff_pct <= 5 and (foreign_net > 0 or trust_net > 0):
                    watchlist.append(f"{code} {name}（盤整區，法人買超）")
                elif diff_pct < -20 and foreign_net > 0:
                    watchlist.append(f"{code} {name}（低檔，外資買超，觀察是否摸底）")
            elif price:
                detail += f"\n    股價 {price}（月線資料不足）"

            lines.append(detail)

    if etfs:
        lines.append("\nETF：")
        for s in etfs[:5]:
            lines.append(f"  {s['code']} {s['name']} | 三大 {s['total']:+,} | 外資 {s['foreign']:+,}")

    return '\n'.join(lines), watchlist


# ═══════════════════════════════════════════════════════
#  主程式
# ═══════════════════════════════════════════════════════

def main():
    print(f"=== 每日台股分析 {TODAY} ===\n")

    # ── 抓資料 ──
    print("[1/5] 匯率...")
    usd_today, usd_prev, usd_chg = fetch_fx('USDTWD=X')
    if not usd_today:
        usd_today, usd_prev, usd_chg = fetch_usdtwd_bot()
    cny_today, cny_prev, cny_chg = fetch_fx('CNYTWD=X')
    krw_today, krw_prev, krw_chg = fetch_fx('KRWTWD=X')

    print("[2/5] 指數 & 期貨...")
    taiex = fetch_taiex()
    tx = fetch_tx_futures()
    spread = (tx - taiex) if (tx and taiex) else None

    print("[3/5] 三大法人...")
    inst = fetch_institutional()
    foreign = next((v for k, v in inst.items() if '外資' in k and '陸資' in k and '自行' not in k), None)
    trust = next((v for k, v in inst.items() if '投信' in k), None)
    dealer = next((v for k, v in inst.items() if '自營' in k and '自行' in k), None)

    print("[4/5] 法人買超個股...")
    top_stocks = fetch_top_stocks()

    print("[5/5] 美股...")
    us_market = fetch_us_market()

    # ── 按照 CLAUDE.md 三件事分析 ──
    print("\n開始分析...\n")

    # 第一件事：匯率
    fx_analysis, fx_direction, fx_signals = analyze_fx(
        usd_today, usd_prev, usd_chg,
        cny_today, cny_prev, cny_chg,
        krw_today, krw_prev, krw_chg
    )

    # 第二件事：期貨
    futures_analysis = analyze_futures(taiex, tx, spread, us_market)

    # 第三件事：籌碼
    chips_analysis = analyze_chips(inst, foreign, trust, dealer, top_stocks)
    stock_detail, watchlist = analyze_stock_detail(top_stocks)

    # 外資 vs 匯率一致性
    consistency = ""
    if foreign and usd_chg is not None:
        if foreign['diff'] > 0 and usd_chg < -0.05:
            consistency = "✅ 外資買超 + 台幣升值，方向一致，籌碼可信度高"
        elif foreign['diff'] > 0 and usd_chg > 0.05:
            consistency = "⚠ 外資買超但台幣貶值，方向矛盾，可能是期貨操作非現貨"
        elif foreign['diff'] < 0 and usd_chg > 0.05:
            consistency = "⚠ 外資賣超 + 台幣貶值，方向一致，偏空"
        elif foreign['diff'] < 0 and usd_chg < -0.05:
            consistency = "💡 外資賣超但台幣升值，可能有其他資金流入"

    # ── 綜合結論（按照 CLAUDE.md 格式）──
    bullish_count = 0
    bearish_count = 0

    # 匯率
    if fx_direction == "升值":
        bullish_count += 1
    elif fx_direction == "貶值":
        bearish_count += 1
    if "三幣齊升" in fx_signals:
        bullish_count += 1
    elif "三幣齊貶" in fx_signals:
        bearish_count += 1

    # 期貨
    if spread is not None:
        if spread > 100:
            bullish_count += 1
        elif spread < -100 and not (6 <= MONTH <= 8):
            bearish_count += 1

    # 法人
    if foreign and foreign['diff'] > 0:
        bullish_count += 1
    elif foreign and foreign['diff'] < 0:
        bearish_count += 1
    if trust and trust['diff'] > 0:
        bullish_count += 0.5
    elif trust and trust['diff'] < 0:
        bearish_count += 0.5

    if bullish_count > bearish_count + 1:
        direction = "偏多"
    elif bearish_count > bullish_count + 1:
        direction = "偏空"
    elif bullish_count > bearish_count:
        direction = "略偏多"
    elif bearish_count > bullish_count:
        direction = "略偏空"
    else:
        direction = "中性"

    # 觀察標的
    watch_str = '、'.join(watchlist[:3]) if watchlist else "無明確標的"

    # 進場條件
    if direction in ["偏多", "略偏多"]:
        condition = "站上月線才進場，開高不追，等拉回量縮再找買點"
    elif direction in ["偏空", "略偏空"]:
        condition = "今天別衝，等方向明確再動作"
    else:
        condition = "訊號混雜，先看5分鐘再決定"

    conclusion = f"方向{direction}。觀察{watch_str}。{condition}。"

    # ── 美股表格 ──
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

    # ── 匯率表格 ──
    fx_table = f"""| 幣別 | 今日 | 前日 | 變動 |
|------|------|------|------|
| USD/TWD | {usd_today or '—'} | {usd_prev or '—'} | {f'{usd_chg:+.4f}' if usd_chg is not None else '—'} |
| CNY/TWD | {cny_today or '—'} | {cny_prev or '—'} | {f'{cny_chg:+.4f}' if cny_chg is not None else '—'} |
| KRW/TWD | {krw_today or '—'} | {krw_prev or '—'} | {f'{krw_chg:+.4f}' if krw_chg is not None else '—'} |"""

    # ── 組合報告（按照 CLAUDE.md 三件事結構）──
    report = f"""
## {TODAY}

### 昨晚美股
{us_table}

---

### 第一件事：匯率判讀

{fx_table}

{fx_analysis}

---

### 第二件事：期貨

| 項目 | 數字 |
|------|------|
| 加權指數 | {f'{taiex:,.2f}' if taiex else '取得失敗'} |
| 台指期 | {f'{tx:,.2f}' if tx else '取得失敗'} |
| 期現價差 | {f'{spread:+.0f} 點' if spread is not None else '取得失敗'} |

{futures_analysis}

---

### 第三件事：法人籌碼

{chips_analysis}

{consistency}

#### 法人買超個股分析
{stock_detail}

---

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
    print(f"\n✅ 已寫入 {obs_file}")

    # ── 寫入 summary.txt（推播用，精簡版）──
    foreign_str = fmt_money(foreign['diff']) if foreign else "取得失敗"
    trust_str = fmt_money(trust['diff']) if trust else "取得失敗"

    us_brief = ""
    if us_market.get('S&P 500'):
        sp = us_market['S&P 500']
        us_brief = f"🇺🇸 S&P500 {sp['close']:,.0f}（{sp['pct']:+.2f}%）"
    if us_market.get('費半'):
        sox = us_market['費半']
        us_brief += f" | 費半 {sox['pct']:+.2f}%"

    # 匯率精簡
    if usd_chg is not None and usd_chg < -0.1:
        fx_brief = f"台幣升值{abs(usd_chg):.3f}（明顯）"
    elif usd_chg is not None and usd_chg > 0.1:
        fx_brief = f"台幣貶值{usd_chg:.3f}（明顯）"
    elif usd_chg is not None:
        fx_brief = f"平盤（{usd_chg:+.3f}）"
    else:
        fx_brief = "未取得"

    # 三幣精簡
    if "三幣齊升" in fx_signals:
        three_brief = "三幣齊升，資金流入亞洲"
    elif "三幣齊貶" in fx_signals:
        three_brief = "三幣齊貶，資金外流"
    elif "僅台幣升" in fx_signals:
        three_brief = "僅台幣升，可能拋匯"
    elif "韓元獨升" in fx_signals:
        three_brief = "韓元獨升，小心外資賣台買韓"
    else:
        three_brief = "走向分歧"

    # 買超前5
    if top_stocks:
        individual = [s for s in top_stocks if not s['code'].startswith('00')][:5]
        top5 = '\n'.join(
            f"  {s['code']} {s['name']} 外{fmt_money(s['foreign'])} 投{fmt_money(s['trust'])}"
            for s in individual
        )
        top5_str = f"\n📈 買超前5：\n{top5}"
    else:
        top5_str = ""

    summary = (
        f"📅 {TODAY} 台股分析\n"
        f"{us_brief}\n"
        f"💱 {fx_brief}\n"
        f"🌏 {three_brief}\n"
        f"🏦 外資 {foreign_str} | 投信 {trust_str}\n"
        f"📊 期現價差 {f'{spread:+.0f}點' if spread is not None else '取得失敗'}\n"
        f"\n📝 {conclusion}"
        f"{top5_str}"
    )

    with open('summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)
    print(f"✅ 已寫入 summary.txt")


if __name__ == '__main__':
    main()
