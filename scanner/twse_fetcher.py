"""TWSE OpenAPI fetcher
端點: https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=YYYYMMDD&stockNo=XXXX&response=json

每次回傳該「月份」所有交易日的 OHLCV
rate limit: 每次 query 間隔 ≥ 3 秒 (保守)
"""
from __future__ import annotations
import json
import ssl
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

DB_PATH = "回測_0050還原數據.db"
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
SLEEP_SEC = 3.5

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36"


def _tw_date_to_iso(tw: str) -> str:
    """'115/04/17' -> '2026-04-17'"""
    y, m, d = tw.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def fetch_twse_month(stock_no: str, year: int, month: int) -> list[dict]:
    """抓某月份原始 OHLCV (未還原)"""
    ym = f"{year:04d}{month:02d}01"
    url = f"{TWSE_URL}?date={ym}&stockNo={stock_no}&response=json"
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        print(f"  [{stock_no} {year}-{month:02d}] HTTP error: {e}")
        return []
    if data.get("stat") != "OK":
        return []
    rows = []
    for r in data.get("data", []):
        # 欄位: 日期 / 成交股數 / 成交金額 / 開盤 / 最高 / 最低 / 收盤 / 漲跌 / 成交筆數
        try:
            iso = _tw_date_to_iso(r[0])
            vol = int(r[1].replace(",", ""))
            o = float(r[3].replace(",", ""))
            h = float(r[4].replace(",", ""))
            lo = float(r[5].replace(",", ""))
            c = float(r[6].replace(",", ""))
            rows.append({"date": iso, "open": o, "high": h, "low": lo, "close": c, "volume": vol})
        except (ValueError, IndexError):
            continue
    return rows


def iter_months(start: date, end: date) -> Iterable[tuple[int, int]]:
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            m = 1
            y += 1


def update_twse_stock(ticker: str, stock_no: str, start_date: str, end_date: str) -> int:
    """抓 start_date ~ end_date 的原始資料, UPSERT 進 raw table"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_prices_raw ("
        "ticker TEXT NOT NULL, date TEXT NOT NULL, "
        "open REAL, high REAL, low REAL, close REAL, volume INTEGER, "
        "PRIMARY KEY(ticker, date))"
    )
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    inserted = 0
    for y, m in iter_months(start, end):
        rows = fetch_twse_month(stock_no, y, m)
        n = 0
        for r in rows:
            if start_date <= r["date"] <= end_date:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_prices_raw "
                    "(ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                    (ticker, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]),
                )
                n += 1
        print(f"  [{ticker} {y}-{m:02d}] +{n} 筆")
        inserted += n
        conn.commit()
        time.sleep(SLEEP_SEC)
    conn.close()
    return inserted


if __name__ == "__main__":
    # 先補 00631L 分割後到今天的原始資料
    n = update_twse_stock("00631L.TW", "00631L", "2026-03-31", "2026-04-17")
    print(f"\n完成: +{n} 筆")
    # 驗證
    conn = sqlite3.connect(DB_PATH)
    print("\n=== 00631L.TW raw 最新 15 筆 ===")
    for r in conn.execute(
        "SELECT * FROM daily_prices_raw WHERE ticker='00631L.TW' ORDER BY date DESC LIMIT 15"
    ).fetchall():
        print(r)
