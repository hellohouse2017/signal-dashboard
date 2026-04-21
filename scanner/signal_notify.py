#!/usr/bin/env python3
"""每日進出場信號 TG 通知 — 本地版

取代壞掉的 Vercel API + GitHub Actions，直接讀本地 DB/JSON 計算信號。

用法:
  python3 signal_notify.py              # 計算信號 + 發 TG
  python3 signal_notify.py --dry-run    # 只顯示結果，不發 TG
  python3 signal_notify.py --no-alert   # 不發 TG

條件 (h_v2_1 策略):
  出場 4 取 3 + 連續 2 天:
    c1: 0050 < MA60 且 < MA120
    c2: VIX > 28 且 VIX9D > 28 且 VIX3M > 28
    c3: 00631L 從歷史高點回撤 > 10%
    c4: SMH < MA30 且 < MA60
  回場:
    n_conds < 3 (不是災難)

數據來源:
  - 0050, 00631L: daily_prices_raw (SQLite)
  - VIX, VIX9D, VIX3M, SMH: JSON 檔案

狀態追蹤:
  - scanner/.signal_state.json: 記錄連續天數、上次狀態
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "回測_0050還原數據.db"
STATE_FILE = SCRIPT_DIR / ".signal_state.json"

# JSON 資料檔
VIX_JSON = SCRIPT_DIR / "VIX歷史.json"
VIX9D_JSON = SCRIPT_DIR / "VIX9D歷史.json"
VIX3M_JSON = SCRIPT_DIR / "VIX3M歷史.json"
SMH_JSON = SCRIPT_DIR / "SMH歷史.json"


# ── 工具函式 ─────────────────────────────────────
def sma(data: dict[str, float], dates: list[str], window: int) -> dict[str, float]:
    """計算簡單移動平均"""
    result = {}
    vals = []
    for d in dates:
        if d in data:
            vals.append(data[d])
        if len(vals) >= window:
            result[d] = sum(vals[-window:]) / window
    return result


def expanding_max(data: dict[str, float], dates: list[str]) -> dict[str, float]:
    """計算歷史最高值"""
    result = {}
    mx = None
    for d in dates:
        if d in data:
            mx = max(mx, data[d]) if mx is not None else data[d]
            result[d] = mx
    return result


def load_json_prices(path: Path) -> dict[str, float]:
    """讀取 JSON 價格檔"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_db_prices(conn: sqlite3.Connection, ticker: str,
                   limit: int = 200) -> dict[str, float]:
    """從 DB 讀取最近 N 天收盤價"""
    rows = conn.execute(
        "SELECT date, close FROM daily_prices_raw "
        "WHERE ticker=? ORDER BY date DESC LIMIT ?",
        (ticker, limit),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ── TG ───────────────────────────────────────────
def _load_tg_credentials() -> tuple[str, str]:
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


def send_tg(message: str) -> bool:
    token, chat_id = _load_tg_credentials()
    if not token or not chat_id:
        print("  ⚠️ TG 未設定，跳過通知")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print("  📱 TG 通知已送出")
                return True
            print(f"  ❌ TG 回應異常: {result}")
            return False
    except Exception as e:
        print(f"  ❌ TG 發送失敗: {e}")
        return False


# ── 狀態追蹤 ─────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"consec_days": 0, "last_status": "normal", "last_date": ""}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── 信號計算 ─────────────────────────────────────
def calc_signal() -> dict:
    """計算當日信號（h_v2_1 策略條件）"""

    # 讀資料
    conn = sqlite3.connect(str(DB_PATH))
    p0050_all = load_db_prices(conn, "0050.TW", 200)
    p631l_all = load_db_prices(conn, "00631L.TW", 500)
    conn.close()

    vix_all = load_json_prices(VIX_JSON)
    vix9d_all = load_json_prices(VIX9D_JSON)
    vix3m_all = load_json_prices(VIX3M_JSON)
    smh_all = load_json_prices(SMH_JSON)

    if not p0050_all:
        return {"error": "0050 資料為空"}

    # 按日期排序
    all_dates = sorted(set(
        list(p0050_all.keys()) + list(vix_all.keys()) + list(smh_all.keys())
    ))

    # 最新日期
    latest_0050_date = max(p0050_all.keys())
    latest_vix_date = max(vix_all.keys()) if vix_all else "N/A"
    latest_smh_date = max(smh_all.keys()) if smh_all else "N/A"

    # 計算 MA
    ma60_0050 = sma(p0050_all, all_dates, 60)
    ma120_0050 = sma(p0050_all, all_dates, 120)
    ma30_smh = sma(smh_all, all_dates, 30)
    ma60_smh = sma(smh_all, all_dates, 60)
    exp_max_631l = expanding_max(p631l_all, all_dates)

    # 取最新值
    p50 = p0050_all.get(latest_0050_date)
    ma60 = ma60_0050.get(latest_0050_date)
    ma120 = ma120_0050.get(latest_0050_date)

    vix = vix_all.get(latest_vix_date)
    vix9d = vix9d_all.get(latest_vix_date) if vix9d_all else None
    vix3m = vix3m_all.get(latest_vix_date) if vix3m_all else None

    smh = smh_all.get(latest_smh_date)
    s30 = ma30_smh.get(latest_smh_date)
    s60 = ma60_smh.get(latest_smh_date)

    # 00631L 最新 (取和 0050 同日或最近)
    p631l = p631l_all.get(latest_0050_date)
    mx631l = exp_max_631l.get(latest_0050_date)

    # 4 個出場條件 (h_v2_1)
    c1 = bool(p50 and ma60 and ma120 and p50 < ma60 and p50 < ma120)
    c2 = bool(vix and vix9d and vix3m and vix > 28 and vix9d > 28 and vix3m > 28)
    c3 = bool(p631l and mx631l and (p631l / mx631l - 1) < -0.10)
    c4 = bool(smh and s30 and s60 and smh < s30 and smh < s60)

    n_conds = sum([c1, c2, c3, c4])
    disaster = n_conds >= 3

    # 連續天數追蹤
    state = load_state()
    if disaster:
        state["consec_days"] += 1
    else:
        state["consec_days"] = 0

    consec = state["consec_days"]
    exit_confirmed = disaster and consec >= 2
    entry_safe = not disaster and n_conds <= 1  # 寬鬆安全: ≤1 條件

    if exit_confirmed:
        status = "exit_confirmed"
    elif disaster:
        status = "exit_warning"
    elif n_conds >= 2:
        status = "exit_caution"
    elif entry_safe and state.get("last_status") in ("exit_confirmed", "exit_warning"):
        status = "entry_safe"
    else:
        status = "normal"

    state["last_status"] = status
    state["last_date"] = latest_0050_date
    save_state(state)

    conditions = [
        {"name": "0050 < MA60 且 < MA120", "met": c1,
         "detail": f"0050={p50:.2f}, MA60={ma60:.2f}, MA120={ma120:.2f}" if p50 and ma60 and ma120 else "資料不足"},
        {"name": "VIX/9D/3M 全 > 28", "met": c2,
         "detail": f"VIX={vix:.1f}, 9D={vix9d:.1f}, 3M={vix3m:.1f}" if vix and vix9d and vix3m else "資料不足"},
        {"name": "00631L 回撤 > 10%", "met": c3,
         "detail": f"631L={p631l:.2f}, 高點={mx631l:.2f}, 跌幅={((p631l/mx631l-1)*100):.1f}%" if p631l and mx631l else "資料不足"},
        {"name": "SMH < MA30 且 < MA60", "met": c4,
         "detail": f"SMH={smh:.2f}, MA30={s30:.2f}, MA60={s60:.2f}" if smh and s30 and s60 else "資料不足"},
    ]

    return {
        "status": status,
        "n_conds": n_conds,
        "consec_days": consec,
        "conditions": conditions,
        "prices": {
            "0050": round(p50, 2) if p50 else None,
            "00631L": round(p631l, 2) if p631l else None,
            "VIX": round(vix, 1) if vix else None,
            "SMH": round(smh, 2) if smh else None,
        },
        "dates": {
            "0050": latest_0050_date,
            "VIX": latest_vix_date,
            "SMH": latest_smh_date,
        },
    }


# ── 訊息格式 ─────────────────────────────────────
def format_message(sig: dict) -> str:
    """組合 TG 訊息"""
    today_str = date.today().isoformat()
    status = sig["status"]
    n = sig["n_conds"]
    consec = sig["consec_days"]
    p = sig["prices"]

    # 條件明細
    cond_lines = []
    for c in sig["conditions"]:
        icon = "🔴" if c["met"] else "⚪"
        cond_lines.append(f"  {icon} {c['name']}")

    conds_text = "\n".join(cond_lines)

    if status == "exit_confirmed":
        header = "🚨🚨🚨 災難出場確認！立即賣出！"
        action = (
            f"🛡️ 出場條件：{n}/4（連續 {consec} 天 ≥3）\n\n"
            f"👉 全部賣出 0050/00631L，持有現金\n"
            f"👉 等待安全進場信號再買回"
        )
    elif status == "exit_warning":
        header = f"⚠️⚠️ 出場預警！第 {consec} 天亮燈"
        action = (
            f"🛡️ 出場條件：{n}/4（再 1 天確認出場）\n\n"
            f"👉 繼續持有，但明天務必再檢查\n"
            f"💡 連續 2 天 ≥3 條件才觸發出場"
        )
    elif status == "exit_caution":
        header = f"⚠️ 出場注意：{n}/4 條件亮燈"
        action = (
            f"👉 繼續持有，密切關注\n"
            f"💡 再多 1 個條件就進入預警"
        )
    elif status == "entry_safe":
        header = "✅ 安全進場！可加碼買入"
        action = (
            f"🟢 進場條件：{n}/4（≤1 觸發）\n\n"
            f"👉 市場已安全，回場 + 加碼"
        )
    else:
        header = "😎 每日信號：正常持有"
        action = f"✅ 出場 {n}/4 | 不需操作"

    price_line = f"📈 0050：{p.get('0050', 'N/A')} 元 | 00631L：{p.get('00631L', 'N/A')} 元"
    vix_line = f"📊 VIX：{p.get('VIX', 'N/A')} | SMH：{p.get('SMH', 'N/A')}"

    msg = f"{header}\n\n{price_line}\n{vix_line}\n{action}\n\n📋 條件明細：\n{conds_text}\n\n📅 {today_str} (數據至 {sig['dates']['0050']})"

    return msg


# ── 主程式 ────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="每日進出場信號 TG 通知")
    parser.add_argument("--dry-run", action="store_true", help="只顯示結果，不發 TG")
    parser.add_argument("--no-alert", action="store_true", help="不發 TG")
    args = parser.parse_args()

    print("=" * 50)
    print(f"  📡 信號檢查  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    sig = calc_signal()

    if "error" in sig:
        print(f"  ❌ 錯誤: {sig['error']}")
        return 1

    # 顯示結果
    status_icons = {
        "exit_confirmed": "🚨 出場確認",
        "exit_warning": "⚠️ 出場預警",
        "exit_caution": "⚠️ 出場注意",
        "entry_safe": "✅ 安全進場",
        "normal": "😎 正常",
    }
    print(f"\n  狀態: {status_icons.get(sig['status'], sig['status'])}")
    print(f"  條件: {sig['n_conds']}/4 | 連續: {sig['consec_days']} 天")
    for c in sig["conditions"]:
        icon = "🔴" if c["met"] else "⚪"
        print(f"    {icon} {c['name']} — {c['detail']}")

    msg = format_message(sig)
    print(f"\n{'─' * 50}")
    print(msg)
    print(f"{'─' * 50}")

    # 發送 TG
    if not args.dry_run and not args.no_alert:
        send_tg(msg)
    elif args.dry_run:
        print("\n  [DRY RUN] 未發送 TG")

    return 0


if __name__ == "__main__":
    sys.exit(main())
