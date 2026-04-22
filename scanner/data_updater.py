#!/usr/bin/env python3
"""每日資料更新器 — 一鍵更新回測所需的全部市場數據

用法:
  python3 data_updater.py              # 正常更新
  python3 data_updater.py --dry-run    # 只顯示會做什麼，不寫入
  python3 data_updater.py --force      # 強制從30天前重抓 (修補用)
  python3 data_updater.py --git-push   # 更新後自動 git commit+push JSON
  python3 data_updater.py --no-alert   # 不發 TG 警報

部署:
  Mini (排程): python3 data_updater.py --git-push  (launchd 06:30)
  Air  (手動): git pull && python3 data_updater.py  (補 DB 差量)

更新項目:
  1. TWSE: 0050.TW, 00631L.TW → daily_prices_raw (SQLite)
  2. yfinance: VIX, VIX9D, VIX3M, SMH → JSON 檔案 (走 git 同步)
  3. yfinance: 2330.TW → daily_prices_raw (備用比對)

TG 警報:
  更新失敗時自動發 TG 通知。需要設定環境變數:
  - TG_BOT_TOKEN: Telegram Bot token
  - TG_CHAT_ID: Telegram Chat ID
  或在 scanner/.env 中設定。

設計原則:
  - 冪等 (重複跑不會壞資料)
  - 自動偵測各來源最後日期，只補差量
  - 控制 rate limit (TWSE 3.5s/次)
  - 失敗時自動 TG 警報
  - 適合 cron / launchd 每日自動執行
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.parse

# ── 設定 ──────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "回測_0050還原數據.db"
CORP_ACTIONS_JSON = SCRIPT_DIR / "corporate_actions.json"

# TWSE 標的: (ticker_in_db, twse_stock_no, 描述)
TWSE_TARGETS = [
    ("0050.TW", "0050", "元大台灣50"),
    ("00631L.TW", "00631L", "元大台灣50正2"),
]

# yfinance JSON 標的: (yf_ticker, json_filename, 描述, always_full)
# always_full=True → 每次全量重建 (配息標的 adjusted close 會回溯變動)
YF_JSON_TARGETS = [
    ("^VIX", "VIX歷史.json", "VIX", False),
    ("^VIX9D", "VIX9D歷史.json", "VIX9D", False),
    ("^VIX3M", "VIX3M歷史.json", "VIX3M", False),
    ("SMH", "SMH歷史.json", "SMH", True),  # SMH 季配息，增量更新會漂移
]

# yfinance DB 標的 (寫入 daily_prices_raw)
YF_DB_TARGETS = [
    ("2330.TW", "2330.TW", "台積電"),
]

# TWSE API 限流
TWSE_SLEEP = 3.5


# ── TG 警報 ──────────────────────────────────────
def _load_tg_credentials() -> tuple[str, str]:
    """從環境變數或 .env 檔讀取 TG credentials"""
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        env_path = SCRIPT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "TG_BOT_TOKEN" and not token:
                    token = v
                elif k == "TG_CHAT_ID" and not chat_id:
                    chat_id = v
    return token, chat_id


def send_tg_alert(message: str) -> bool:
    """發送 TG 警報通知"""
    token, chat_id = _load_tg_credentials()
    if not token or not chat_id:
        print("  ⚠️ TG 未設定 (缺少 TG_BOT_TOKEN 或 TG_CHAT_ID)，跳過警報")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("  📱 TG 警報已送出")
                return True
            else:
                print(f"  ❌ TG 回應異常: {result}")
                return False
    except Exception as e:
        print(f"  ❌ TG 發送失敗: {e}")
        return False

# ── corporate_actions 同步 ────────────────────────
CORP_ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    action TEXT NOT NULL,
    ratio REAL,
    cash_dividend REAL,
    note TEXT,
    PRIMARY KEY (ticker, ex_date, action)
)
"""


def sync_corporate_actions(conn: sqlite3.Connection, dry_run: bool) -> dict[str, int]:
    """將 corporate_actions.json 同步到 DB (JSON 為 source of truth)

    行為:
      - JSON 有的: upsert 進 DB
      - DB 有但 JSON 沒有的: 刪除 (避免兩邊漂移)
    回傳: {"upserted": N, "deleted": M}
    """
    if not CORP_ACTIONS_JSON.exists():
        print(f"  ⚠️ 找不到 {CORP_ACTIONS_JSON.name}, 跳過事件同步")
        return {"upserted": 0, "deleted": 0}

    events = json.loads(CORP_ACTIONS_JSON.read_text(encoding="utf-8"))
    conn.execute(CORP_ACTIONS_SCHEMA)

    json_keys = {(e["ticker"], e["ex_date"], e["action"]) for e in events}
    db_keys = {
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT ticker, ex_date, action FROM corporate_actions"
        )
    }
    to_delete = db_keys - json_keys

    print(f"  📋 JSON {len(events)} 筆 / DB {len(db_keys)} 筆", end="")
    if dry_run:
        print(f" [DRY RUN] 會 upsert {len(events)}, 刪除 {len(to_delete)}")
        return {"upserted": -1, "deleted": -1}
    print()

    upserted = 0
    for e in events:
        conn.execute(
            "INSERT OR REPLACE INTO corporate_actions "
            "(ticker, ex_date, action, ratio, cash_dividend, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (e["ticker"], e["ex_date"], e["action"],
             e.get("ratio"), e.get("cash_dividend"), e.get("note")),
        )
        upserted += 1

    deleted = 0
    for k in to_delete:
        conn.execute(
            "DELETE FROM corporate_actions WHERE ticker=? AND ex_date=? AND action=?",
            k,
        )
        deleted += 1

    conn.commit()
    print(f"    → upsert {upserted} 筆, 刪除 {deleted} 筆")
    return {"upserted": upserted, "deleted": deleted}


# ── TWSE 更新 ──────────────────────────────────────
def get_last_date_in_db(conn: sqlite3.Connection, ticker: str) -> str | None:
    """取得 daily_prices_raw 中某 ticker 的最新日期"""
    row = conn.execute(
        "SELECT MAX(date) FROM daily_prices_raw WHERE ticker=?", (ticker,)
    ).fetchone()
    return row[0] if row and row[0] else None


def update_twse(conn: sqlite3.Connection, dry_run: bool, force_start: str | None) -> dict[str, int]:
    """更新 TWSE 標的到 daily_prices_raw"""
    from twse_fetcher import fetch_twse_month, iter_months

    today = date.today()
    results = {}

    for ticker, stock_no, desc in TWSE_TARGETS:
        last = get_last_date_in_db(conn, ticker)
        if force_start:
            start_date = force_start
        elif last:
            # 從最後一天的下一天開始
            start_date = (datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        else:
            start_date = "2010-01-01"

        if start_date > today.isoformat():
            print(f"  ✅ {desc} ({ticker}): 已是最新 (至 {last})")
            results[ticker] = 0
            continue

        print(f"  📥 {desc} ({ticker}): {start_date} → {today.isoformat()}", end="")
        if dry_run:
            print(" [DRY RUN]")
            results[ticker] = -1
            continue
        print()

        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        inserted = 0
        for y, m in iter_months(start, today):
            rows = fetch_twse_month(stock_no, y, m)
            n = 0
            for r in rows:
                if start_date <= r["date"] <= today.isoformat():
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_prices_raw "
                        "(ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                        (ticker, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]),
                    )
                    n += 1
            if n > 0:
                print(f"    [{ticker} {y}-{m:02d}] +{n} 筆")
            inserted += n
            conn.commit()
            time.sleep(TWSE_SLEEP)

        new_last = get_last_date_in_db(conn, ticker)
        print(f"    → 完成 +{inserted} 筆 (至 {new_last})")
        results[ticker] = inserted

    return results


# ── yfinance JSON 更新 ─────────────────────────────
def update_yf_json(dry_run: bool, force_start: str | None) -> dict[str, int]:
    """更新 yfinance 標的到 JSON 檔"""
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠️ yfinance 未安裝, 跳過 JSON 更新")
        return {}

    results = {}
    today = date.today()

    for yf_ticker, filename, desc, always_full in YF_JSON_TARGETS:
        json_path = SCRIPT_DIR / filename
        existing: dict[str, float] = {}

        # 配息標的必須全量重建 (adjusted close 會回溯變動)
        if json_path.exists() and not always_full:
            existing = json.loads(json_path.read_text())

        if always_full:
            fetch_start = "2004-01-01"
        elif force_start:
            fetch_start = force_start
        elif existing:
            last = max(existing.keys())
            fetch_start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        else:
            fetch_start = "2004-01-01"

        # yfinance end 不含當天，所以+1
        fetch_end = (today + timedelta(days=1)).isoformat()

        mode = f"全量重建" if always_full else fetch_start
        print(f"  📥 {desc} ({yf_ticker}): {mode} → {today.isoformat()}", end="")
        if dry_run:
            print(" [DRY RUN]")
            results[yf_ticker] = -1
            continue
        print()

        try:
            df = yf.download(yf_ticker, start=fetch_start, end=fetch_end, progress=False)
            if df.empty:
                print(f"    ⚠️ 無新資料")
                results[yf_ticker] = 0
                continue

            new_count = 0
            for idx, row in df.iterrows():
                d = idx.strftime("%Y-%m-%d")
                close_val = float(row["Close"].iloc[0]) if hasattr(row["Close"], "iloc") else float(row["Close"])
                if d not in existing or existing[d] != round(close_val, 4):
                    existing[d] = round(close_val, 4)
                    new_count += 1

            # 寫入排序後的 JSON
            sorted_data = dict(sorted(existing.items()))
            json_path.write_text(json.dumps(sorted_data, indent=None, separators=(",", ":")))

            new_last = max(sorted_data.keys())
            print(f"    → 完成 +{new_count} 筆 (至 {new_last}, 總 {len(sorted_data)} 筆)")
            results[yf_ticker] = new_count

        except Exception as e:
            print(f"    ❌ 錯誤: {e}")
            results[yf_ticker] = -1

    return results


# ── yfinance DB 更新 ───────────────────────────────
def update_yf_db(conn: sqlite3.Connection, dry_run: bool, force_start: str | None) -> dict[str, int]:
    """更新 yfinance 標的到 daily_prices_raw"""
    try:
        import yfinance as yf
    except ImportError:
        print("  ⚠️ yfinance 未安裝, 跳過 DB 更新")
        return {}

    results = {}
    today = date.today()

    for yf_ticker, db_ticker, desc in YF_DB_TARGETS:
        last = get_last_date_in_db(conn, db_ticker)

        if force_start:
            fetch_start = force_start
        elif last:
            fetch_start = (datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        else:
            fetch_start = "2000-01-01"

        if fetch_start > today.isoformat():
            print(f"  ✅ {desc} ({db_ticker}): 已是最新 (至 {last})")
            results[db_ticker] = 0
            continue

        fetch_end = (today + timedelta(days=1)).isoformat()

        print(f"  📥 {desc} ({db_ticker}): {fetch_start} → {today.isoformat()}", end="")
        if dry_run:
            print(" [DRY RUN]")
            results[db_ticker] = -1
            continue
        print()

        try:
            df = yf.download(yf_ticker, start=fetch_start, end=fetch_end, progress=False)
            if df.empty:
                print(f"    ⚠️ 無新資料")
                results[db_ticker] = 0
                continue

            inserted = 0
            for idx, row in df.iterrows():
                d = idx.strftime("%Y-%m-%d")
                def val(col):
                    v = row[col]
                    return float(v.iloc[0]) if hasattr(v, "iloc") else float(v)
                conn.execute(
                    "INSERT OR REPLACE INTO daily_prices_raw "
                    "(ticker, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                    (db_ticker, d, val("Open"), val("High"), val("Low"), val("Close"), int(val("Volume"))),
                )
                inserted += 1
            conn.commit()

            new_last = get_last_date_in_db(conn, db_ticker)
            print(f"    → 完成 +{inserted} 筆 (至 {new_last})")
            results[db_ticker] = inserted

        except Exception as e:
            print(f"    ❌ 錯誤: {e}")
            results[db_ticker] = -1

    return results


# ── Git 同步 ──────────────────────────────────────
def git_push_json() -> bool:
    """將 JSON 變更 commit + push (給 Mini 排程用)"""
    repo_root = SCRIPT_DIR.parent  # 選股策略/
    json_files = [f"scanner/{t[1]}" for t in YF_JSON_TARGETS]
    json_files.append("scanner/corporate_actions.json")

    try:
        # 先 add 指定的 JSON 檔
        subprocess.run(
            ["git", "add"] + json_files,
            cwd=str(repo_root), check=True, capture_output=True,
        )
        # 檢查是否有變更
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(repo_root), capture_output=True,
        )
        if result.returncode == 0:
            print("  ℹ️ JSON 無變更，跳過 commit")
            return True

        # Commit
        today_str = date.today().isoformat()
        subprocess.run(
            ["git", "commit", "-m", f"📊 data_updater: JSON 更新 {today_str}"],
            cwd=str(repo_root), check=True, capture_output=True,
        )
        # Pull --rebase (防止 Air 端有新 commit 導致 push 失敗)
        # 失敗不阻擋 push（可能只是網路暫時問題）
        pull_result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=str(repo_root), capture_output=True,
        )
        if pull_result.returncode != 0:
            stderr = pull_result.stderr.decode().strip()[:200] if pull_result.stderr else ""
            print(f"  ⚠️ git pull --rebase 失敗（繼續嘗試 push）: {stderr}")

        # Push
        subprocess.run(
            ["git", "push"],
            cwd=str(repo_root), check=True, capture_output=True,
        )
        print("  ✅ Git commit + push 完成")
        return True

    except subprocess.CalledProcessError as e:
        print(f"  ❌ Git 操作失敗: {e}")
        if e.stderr:
            print(f"     {e.stderr.decode().strip()[:200]}")
        return False


# ── 主程式 ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="每日市場資料更新器")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會做什麼，不實際寫入")
    parser.add_argument("--force", action="store_true", help="強制從30天前開始重抓")
    parser.add_argument("--twse-only", action="store_true", help="只更新 TWSE")
    parser.add_argument("--yf-only", action="store_true", help="只更新 yfinance")
    parser.add_argument("--git-push", action="store_true", help="更新後自動 git commit+push JSON")
    parser.add_argument("--no-alert", action="store_true", help="不發 TG 警報")
    args = parser.parse_args()

    force_start = None
    if args.force:
        force_start = (date.today() - timedelta(days=30)).isoformat()

    print("=" * 60)
    print(f"  📊 每日資料更新器  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  DB: {DB_PATH}")
    if args.dry_run:
        print("  ⚠️ DRY RUN 模式 — 不會寫入任何資料")
    if args.force:
        print(f"  ⚡ 強制模式 — 從 {force_start} 開始")
    print("=" * 60)

    conn = sqlite3.connect(str(DB_PATH))

    # 確保表存在
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_prices_raw ("
        "ticker TEXT NOT NULL, date TEXT NOT NULL, "
        "open REAL, high REAL, low REAL, close REAL, volume INTEGER, "
        "PRIMARY KEY(ticker, date))"
    )

    all_results = {}

    # 0. 同步 corporate_actions (JSON → DB)
    print("\n[0/3] 事件表同步 (corporate_actions.json → DB)")
    ca_r = sync_corporate_actions(conn, args.dry_run)

    # 1. TWSE
    if not args.yf_only:
        print("\n[1/3] TWSE 台股原始資料")
        twse_r = update_twse(conn, args.dry_run, force_start)
        all_results.update(twse_r)

    # 2. yfinance JSON
    if not args.twse_only:
        print("\n[2/3] yfinance 指標 (JSON)")
        yf_json_r = update_yf_json(args.dry_run, force_start)
        all_results.update(yf_json_r)

    # 3. yfinance DB
    if not args.twse_only:
        print("\n[3/3] yfinance 個股 (DB)")
        yf_db_r = update_yf_db(conn, args.dry_run, force_start)
        all_results.update(yf_db_r)

    conn.close()

    # 摘要
    print("\n" + "=" * 60)
    print("  📋 更新摘要")
    print("=" * 60)
    total_new = 0
    failures = []
    for k, v in all_results.items():
        status = "DRY RUN" if v == -1 else f"+{v} 筆" if v > 0 else "已是最新"
        print(f"  {k:12s}  {status}")
        if v > 0:
            total_new += v
        if v == -1 and not args.dry_run:
            failures.append(k)
    print(f"\n  總計新增: {total_new} 筆")

    # Git push
    git_ok = True
    if args.git_push and not args.dry_run and total_new > 0:
        print("\n[4/4] Git 同步")
        git_ok = git_push_json()
    elif args.git_push and total_new == 0:
        print("\n  ℹ️ 無新資料，跳過 git push")

    print("=" * 60)

    # TG 警報
    has_errors = len(failures) > 0 or not git_ok
    if has_errors and not args.no_alert and not args.dry_run:
        today_str = date.today().isoformat()
        alert_parts = [f"⚠️ 選股數據更新異常 ({today_str})"]
        if failures:
            alert_parts.append(f"\n❌ 更新失敗: {', '.join(failures)}")
        if not git_ok:
            alert_parts.append("\n❌ Git push 失敗")
        alert_parts.append("\n📋 請檢查 /tmp/data-updater.log")
        send_tg_alert("\n".join(alert_parts))
    elif not has_errors and not args.dry_run:
        # 週一額外發一個確認訊息（平時安靜）
        import calendar
        if date.today().weekday() == 0:  # 週一
            today_str = date.today().isoformat()
            if not args.no_alert:
                send_tg_alert(f"✅ 週一數據檢查正常 ({today_str})\n新增 {total_new} 筆")

    return 0 if not has_errors else 1


if __name__ == "__main__":
    sys.exit(main())
