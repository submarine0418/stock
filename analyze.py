#!/usr/bin/env python3
"""
台股開盤前每日分析腳本
資料來源：台灣銀行、台灣證交所（官方 API，不會被擋）
"""
import os
import requests
import json
from datetime import datetime
import anthropic
import pytz

TW_TZ = pytz.timezone('Asia/Taipei')
TODAY = datetime.now(TW_TZ).strftime('%Y-%m-%d')
HEADERS = {'User-Agent': 'Mozilla/5.0'}


def fetch_usdtwd():
    """台灣銀行 USD/TWD 即期匯率"""
    try:
        url = "https://rate.bot.com.tw/xrt/fltxt/0/USD"
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.encoding = 'utf-8'
        return r.text[:800]
    except Exception as e:
        return f"取得失敗：{e}"


def fetch_institutional():
    """台灣證交所三大法人買賣超"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        data = r.json()
        # 取出關鍵欄位
        rows = data.get('data', [])
        result = []
        for row in rows:
            result.append(' | '.join(row))
        return '\n'.join(result)
    except Exception as e:
        return f"取得失敗：{e}"


def fetch_taifex():
    """期交所台指期日盤收盤（供計算與夜盤比對用）"""
    try:
        url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.encoding = 'utf-8'
        # 只取前 3000 字，避免 token 爆炸
        return r.text[:3000]
    except Exception as e:
        return f"取得失敗：{e}"


def fetch_taiex():
    """加權指數收盤（供計算期現價差用）"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json"
        r = requests.get(url, timeout=15, headers=HEADERS)
        data = r.json()
        return json.dumps(data, ensure_ascii=False)[:2000]
    except Exception as e:
        return f"取得失敗：{e}"


def run_analysis(raw_data: dict) -> str:
    """呼叫 Claude API，將原始資料格式化成分析報告"""
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    prompt = f"""你是台股開盤前分析助理。今天日期：{TODAY}

以下是從官方 API 取得的原始資料，請整理成分析報告。

---
【台灣銀行 USD/TWD 匯率原始資料】
{raw_data['usdtwd']}

【台灣證交所三大法人買賣超】
{raw_data['institutional']}

【期交所台指期資料（節錄）】
{raw_data['taifex']}

【加權指數資料（節錄）】
{raw_data['taiex']}
---

請根據上述資料，輸出以下格式（繁體中文，直接輸出不要加其他說明）：

## {TODAY}

### 匯率
| 項目 | 數字 | 判斷 |
|------|------|------|
| USD/TWD 昨收 | ... | |
| USD/TWD 今日 | ... | |
| 升貶幅度 | ... | 明顯升值／貶值／平盤 |
| 三幣走向 | 資料未取得（需人工查人民幣韓元） | — |

### 台指期
| 項目 | 數字 |
|------|------|
| 日盤結算 | ... |
| 現貨收盤 | ... |
| 正逆價差 | ... |
| 判斷 | 偏多／偏空／中性 |

### 三大法人整體
| 法人 | 買超金額 |
|------|----------|
| 外資 | ... |
| 投信 | ... |
| 自營商 | ... |
| 合計 | ... |

### 三大法人連續買超個股
（此項需人工查詢 Goodinfo 或籌碼K線）

### 今日結論
方向偏OO。觀察重點：XXX。條件才進。

---
"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def main():
    print(f"開始執行每日分析：{TODAY}")

    # 抓原始資料
    raw = {
        'usdtwd':        fetch_usdtwd(),
        'institutional': fetch_institutional(),
        'taifex':        fetch_taifex(),
        'taiex':         fetch_taiex(),
    }

    # 呼叫 Claude 格式化
    analysis = run_analysis(raw)
    print("=== 分析結果 ===")
    print(analysis)

    # 讀現有 observation.md
    obs_file = 'observation.md'
    try:
        with open(obs_file, 'r', encoding='utf-8') as f:
            existing = f.read()
    except FileNotFoundError:
        existing = '# 每日觀察紀錄\n\n---\n\n'

    # 附加新分析
    with open(obs_file, 'w', encoding='utf-8') as f:
        f.write(existing.rstrip() + '\n\n' + analysis + '\n')

    print(f"已寫入 {obs_file}")


if __name__ == '__main__':
    main()
