#!/usr/bin/env python3
"""
全台股自動掃描器 v3 — 純 TWSE/TPEx API，不依賴 yfinance
- TWSE/TPEx 批量 API 取今日 + 歷史量價
- 本地 SQLite 快取歷史（3個月）
- MongoDB Atlas 存掃描結果（Vercel 讀取）
- Telegram 通知命中 + 退場信號
"""
import os, sys, json, time, sqlite3, logging
from datetime import datetime, timedelta
import pandas as pd
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "market_data.db")
LOG_PATH = os.path.join(SCRIPT_DIR, "scanner.log")

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb+srv://hellohouse2017_db_user:WLyx69c32EJAyBGX@bnbbot.virfati.mongodb.net/?retryWrites=true&w=majority&appName=bnbbot")
TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8612152687:AAGY-tkddde9hXjaajnYcybZTKRa5NMqL9Q")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8289066083")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("scanner")

# ===== SQLite =====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_date ON daily_prices(date)")
    conn.commit()
    return conn

def db_has_date(conn, date_str):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM daily_prices WHERE date = ?", (date_str,))
    return c.fetchone()[0] > 100  # 至少100筆才算有資料

def db_trading_days(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date")
    return [r[0] for r in c.fetchall()]

# ===== TWSE API =====

def parse_number(s):
    """解析 TWSE 的數字（有逗號）"""
    try:
        return float(str(s).replace(",", ""))
    except:
        return 0

def fetch_twse_date(date_str):
    """拉某天全部上市股票的量價（TWSE MI_INDEX）
    date_str: YYYYMMDD 格式
    回傳: [(ticker, name, open, high, low, close, volume), ...]
    """
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        if data.get("stat") != "OK":
            return []

        # 找到收盤行情的 table
        stocks = []
        for table in data.get("tables", []):
            fields = table.get("fields", [])
            if "證券代號" in fields and "收盤價" in fields:
                for row in table.get("data", []):
                    code = row[0].strip()
                    if len(code) != 4 or not code.isdigit():
                        continue
                    close_val = parse_number(row[8])  # 收盤價
                    if close_val <= 0:
                        continue
                    stocks.append({
                        "ticker": f"{code}.TW",
                        "name": row[1].strip(),
                        "open": parse_number(row[5]),
                        "high": parse_number(row[6]),
                        "low": parse_number(row[7]),
                        "close": close_val,
                        "volume": int(parse_number(row[2])),  # 成交股數
                    })
                break
        return stocks
    except Exception as e:
        log.error(f"TWSE date {date_str} error: {e}")
        return []

def fetch_tpex_date(date_str):
    """拉某天全部上櫃股票（TPEx）
    date_str: YYYYMMDD → 轉民國年 YYY/MM/DD
    """
    # 轉民國年
    y = int(date_str[:4]) - 1911
    roc_date = f"{y}/{date_str[4:6]}/{date_str[6:8]}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_date}&se=EW&_={int(time.time()*1000)}"
    try:
        r = requests.get(url, timeout=20)
        data = r.json()
        stocks = []
        for row in data.get("aaData", []):
            code = str(row[0]).strip()
            if len(code) != 4 or not code.isdigit():
                continue
            close_val = parse_number(row[2])
            if close_val <= 0:
                continue
            stocks.append({
                "ticker": f"{code}.TWO",
                "name": str(row[1]).strip(),
                "open": parse_number(row[4]),
                "high": parse_number(row[5]),
                "low": parse_number(row[6]),
                "close": close_val,
                "volume": int(parse_number(row[7])),
            })
        return stocks
    except Exception as e:
        log.error(f"TPEx date {date_str} error: {e}")
        return []

def fetch_twse_today_bulk():
    """今日快速版（OpenAPI，不需等收盤）"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        stocks = []
        for item in data:
            code = item.get("Code", "")
            if len(code) != 4 or not code.isdigit():
                continue
            close_val = parse_number(item.get("ClosingPrice", "0"))
            if close_val <= 0:
                continue
            stocks.append({
                "ticker": f"{code}.TW", "name": item.get("Name", ""),
                "open": parse_number(item.get("OpeningPrice", "0")),
                "high": parse_number(item.get("HighestPrice", "0")),
                "low": parse_number(item.get("LowestPrice", "0")),
                "close": close_val,
                "change": parse_number(item.get("Change", "0")),
                "volume": int(parse_number(item.get("TradeVolume", "0"))),
            })
        return stocks
    except Exception as e:
        log.error(f"TWSE today error: {e}")
        return []

def fetch_tpex_today_bulk():
    """今日上櫃快速版"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        stocks = []
        for item in data:
            code = item.get("SecuritiesCompanyCode", "")
            if len(code) != 4 or not code.isdigit():
                continue
            close_val = parse_number(item.get("Close", "0"))
            if close_val <= 0:
                continue
            stocks.append({
                "ticker": f"{code}.TWO", "name": item.get("CompanyName", ""),
                "open": parse_number(item.get("Open", "0")),
                "high": parse_number(item.get("High", "0")),
                "low": parse_number(item.get("Low", "0")),
                "close": close_val,
                "change": parse_number(item.get("Change", "0")),
                "volume": int(parse_number(item.get("TradingShares", "0"))),
            })
        return stocks
    except Exception as e:
        log.error(f"TPEx today error: {e}")
        return []

# ===== 歷史資料回填 =====

def backfill_history(conn, days=4460):
    """從 TWSE/TPEx 回填歷史資料到 SQLite（預設從2014年起）"""
    log.info(f"📥 回填最近 {days} 天歷史（約{days//365}年）...")
    today = datetime.now()
    filled = 0
    skipped = 0
    errors = 0
    est_trading_days = days * 5 // 7  # 粗估交易日

    for d in range(days, 0, -1):
        dt = today - timedelta(days=d)
        if dt.weekday() >= 5:  # 跳週末
            continue
        date_str = dt.strftime("%Y%m%d")
        date_db = dt.strftime("%Y-%m-%d")

        if db_has_date(conn, date_db):
            skipped += 1
            continue

        # 進度
        remaining = est_trading_days - filled - skipped
        eta_min = remaining * 6 / 60  # 每天約6秒
        if filled % 10 == 0:
            log.info(f"   進度: 已填{filled}天, 跳過{skipped}天, 預估剩餘{eta_min:.0f}分鐘")

        # TWSE 上市
        twse = fetch_twse_date(date_str)
        time.sleep(3)  # TWSE 有 rate limit

        # TPEx 上櫃
        tpex = fetch_tpex_date(date_str)
        time.sleep(3)

        all_stocks = twse + tpex
        if not all_stocks:
            errors += 1
            continue

        c = conn.cursor()
        for s in all_stocks:
            c.execute("""
                INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (s["ticker"], date_db, s["open"], s["high"], s["low"], s["close"], s["volume"]))
        conn.commit()
        filled += 1
        log.info(f"   ✅ {date_db}: {len(all_stocks)} 支")

    log.info(f"📥 回填完成：新增 {filled} 天，跳過 {skipped} 天(已有)，錯誤 {errors} 天")
    return filled

# ===== 存今日到 SQLite =====

def save_today(conn, stocks):
    today = datetime.now().strftime("%Y-%m-%d")
    c = conn.cursor()
    saved = 0
    for s in stocks:
        if s["close"] <= 0:
            continue
        c.execute("""
            INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (s["ticker"], today, s["open"], s["high"], s["low"], s["close"], s["volume"]))
        saved += 1
    conn.commit()
    return saved

# ===== 技術指標 + 掃描 =====

def get_history(conn, ticker, min_days=25):
    cutoff = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("SELECT date, close, volume FROM daily_prices WHERE ticker=? AND date>=? ORDER BY date", (ticker, cutoff))
    rows = c.fetchall()
    if len(rows) < min_days:
        return None
    df = pd.DataFrame(rows, columns=["date", "close", "volume"])
    return df

def calc_keys(df):
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    ma20 = close.rolling(20).mean()
    va20 = vol.rolling(20).mean()
    vr = vol / va20.replace(0, 1)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, 1e-10))
    above3 = (close > ma20).rolling(3).sum()
    rsi_min = rsi.rolling(20).min()

    r_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 0
    r_vr = float(vr.iloc[-1]) if pd.notna(vr.iloc[-1]) else 0
    r_a3 = float(above3.iloc[-1]) if pd.notna(above3.iloc[-1]) else 0
    r_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else 0
    r_rmin = float(rsi_min.iloc[-1]) if pd.notna(rsi_min.iloc[-1]) else 50

    above_days = 0
    for j in range(len(close)-1, -1, -1):
        if pd.notna(ma20.iloc[j]) and close.iloc[j] > ma20.iloc[j]:
            above_days += 1
        else:
            break

    return {
        "rsi": r_rsi, "vol_ratio": r_vr, "ma20": r_ma20,
        "above_3d": r_a3, "above_days": above_days,
        "was_oversold": r_rmin < 30,
        "price": float(close.iloc[-1]),
    }

def run_scan(conn, candidates):
    hits, watch = [], []
    for s in candidates:
        df = get_history(conn, s["ticker"])
        if df is None:
            continue
        ind = calc_keys(df)
        keys = sum([ind["rsi"] > 50, ind["vol_ratio"] >= 1.5, ind["above_3d"] >= 3])
        stock = {
            "ticker": s["ticker"], "name": s["name"],
            "price": round(ind["price"], 1), "rsi": round(ind["rsi"], 1),
            "vol_ratio": round(ind["vol_ratio"], 1), "ma20": round(ind["ma20"], 1),
            "above_ma20_days": ind["above_days"],
            "was_oversold": ind["was_oversold"],
            "today_vol_lots": s["volume"] // 1000,
        }
        if keys == 3: hits.append(stock)
        elif keys == 2: watch.append(stock)

    hits.sort(key=lambda x: (-x["vol_ratio"], -x["rsi"]))
    watch.sort(key=lambda x: (-x["vol_ratio"], -x["rsi"]))
    return hits, watch[:30]

# ===== TG =====

def send_tg(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        log.error(f"TG: {e}")

def notify_hits(hits):
    if not hits: return
    msg = f"🔥 <b>全台股掃描命中 ({len(hits)}支)</b>\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    for s in hits[:10]:
        ov = " ⭐曾超賣" if s.get("was_oversold") else ""
        msg += f"🟢 <b>{s['name']}</b> ({s['ticker']})\n"
        msg += f"   💰 ${s['price']} | RSI {s['rsi']} | 量比 {s['vol_ratio']}x{ov}\n"
        msg += f"   MA20 ${s['ma20']} | 連站{s['above_ma20_days']}天 | {s['today_vol_lots']}張\n\n"
    if len(hits) > 10: msg += f"...還有 {len(hits)-10} 支\n"
    msg += "📊 https://signal-dashboard-ashy.vercel.app"
    send_tg(msg)

# ===== MongoDB =====

def push_mongo(hits, watch, meta):
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        db = client["signal_dashboard"]
        db["scan_results"].replace_one({"_id": "latest"}, {
            "_id": "latest", "scan_time": datetime.now().isoformat(),
            "hits": hits, "watchlist": watch, **meta,
        }, upsert=True)
        db["scan_history"].insert_one({
            "scan_time": datetime.now().isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M"),
            "hit_count": len(hits),
            "hit_tickers": [h["ticker"] for h in hits],
        })
        log.info(f"✅ MongoDB: {len(hits)} hits")
    except Exception as e:
        log.error(f"MongoDB: {e}")

# ===== 退場檢查 =====

def check_exits(conn):
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        paper = client["signal_dashboard"]["paper_trading"].find_one({"_id": "main"})
        if not paper or not paper.get("started") or not paper.get("positions"): return

        alerts = []
        for pos in paper["positions"]:
            df = get_history(conn, pos["ticker"])
            if df is None or len(df) < 20: continue
            cur = float(df["close"].iloc[-1])
            ma20 = float(df["close"].astype(float).rolling(20).mean().iloc[-1])
            hp = max(pos.get("high_price", pos["buy_price"]), cur)
            ret = (cur / pos["buy_price"] - 1) * 100
            fh = (cur / hp - 1) * 100
            days = (datetime.now() - datetime.strptime(pos["buy_date"], "%Y-%m-%d")).days

            # D++ 漸進式移動停損
            sig = None
            if ret < 0 and ret <= -6:
                sig = "🔴 固定停損 -6%"
            elif ret >= 0 and ret < 10 and fh <= -10:
                sig = "🟠 移動停損 -10%"
            elif ret >= 10 and ret < 20 and fh <= -8:
                sig = "🟡 移動停損 -8%"
            elif ret >= 20 and ret < 50 and fh <= -10:
                sig = "🟡 移動停損 -10% (飆股區)"
            elif ret >= 50 and fh <= -8:
                sig = "🟡 移動停損 -8% (超飆)"
            elif days >= 3 and ma20 > 0 and cur < ma20 and ret < 0:
                sig = "🔴 破MA20停損"
            if sig:
                alerts.append({"name": pos["name"], "ticker": pos["ticker"], "signal": sig,
                               "buy_price": pos["buy_price"], "current": round(cur, 1),
                               "pnl_pct": round(ret, 1), "days": days})

        if alerts:
            msg = f"🚨 <b>退場信號 ({len(alerts)}支)</b>\n📅 {datetime.now().strftime('%H:%M')}\n\n"
            for a in alerts:
                ps = "+" if a["pnl_pct"] >= 0 else ""
                msg += f"{a['signal']} <b>{a['name']}</b>\n   買${a['buy_price']}→現${a['current']} ({ps}{a['pnl_pct']}%) {a['days']}天\n\n"
            msg += "⚡ 儀表板 → 模擬單 → 賣出"
            send_tg(msg)
    except Exception as e:
        log.error(f"Exit check: {e}")

# ===== Main =====

def main():
    start = time.time()
    log.info("🔍 全台股掃描 v3 (純 TWSE API)")

    now = datetime.now()
    is_market = now.weekday() < 5 and 900 <= now.hour*100+now.minute <= 1400

    # --backfill: 回填歷史
    if "--backfill" in sys.argv:
        conn = init_db()
        backfill_history(conn)  # 預設5年(1826天)
        conn.close()
        return

    if not is_market and "--force" not in sys.argv:
        log.info(f"非交易時間 ({now.strftime('%A %H:%M')})，用 --force 強制")
        return

    conn = init_db()

    # Step 1: 今日數據
    log.info("📡 取全市場今日數據...")
    twse = fetch_twse_today_bulk()
    tpex = fetch_tpex_today_bulk()
    all_stocks = twse + tpex
    saved = save_today(conn, all_stocks)
    log.info(f"   {len(twse)}+{len(tpex)}={len(all_stocks)} 支, SQLite寫入{saved}")

    # 檢查歷史是否足夠
    trading_days = db_trading_days(conn)
    if len(trading_days) < 25:
        log.info(f"⚠️ 歷史不足 ({len(trading_days)}天<25天), 先跑 --backfill")
        backfill_history(conn, days=80)

    # Step 2: 預篩
    candidates = [s for s in all_stocks if s["volume"]//1000 >= 500 and s["close"] >= 10 and s.get("change", 0) >= 0]
    log.info(f"🔎 預篩: {len(all_stocks)} → {len(candidates)}")

    # Step 3: 掃描
    hits, watch = run_scan(conn, candidates)
    elapsed = time.time() - start
    log.info(f"✅ 命中 {len(hits)} | 觀察 {len(watch)} | {elapsed:.1f}秒")

    # 通知 + 存儲
    if hits: notify_hits(hits)
    push_mongo(hits, watch, {"total": len(all_stocks), "filtered": len(candidates), "elapsed": round(elapsed, 1)})

    # 退場檢查
    check_exits(conn)

    conn.close()
    log.info(f"🏁 完成 ({time.time()-start:.1f}秒)")

    # 印結果
    if hits:
        print(f"\n🟢 命中 {len(hits)} 支:")
        for s in hits:
            ov = "⭐" if s["was_oversold"] else ""
            print(f"  {s['name']:<8} {s['ticker']:<10} ${s['price']:>7} RSI{s['rsi']:>5.1f} 量{s['vol_ratio']:>4.1f}x 站{s['above_ma20_days']}天 {ov}")

if __name__ == "__main__":
    main()
