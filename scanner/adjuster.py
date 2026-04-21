"""還原引擎: 以最新日為基準 1.0, 往回推算 adj_factor

規則:
  split (ratio=R): ex_date 當日生效, ex_date 當日起 factor 不變,
    ex_date 之前所有日 factor *= 1/R
    (舊 1 股 = 新 R 股, 舊股價除以 R 才能和新股比較)
  cash_dividend (div=D, 前一日收盤=P_prev):
    ex_date 之前所有日 factor *= (P_prev - D) / P_prev

adj_close(t) = close(t) * factor(t)
adj_open/high/low 同理乘 factor(t)
adj_volume(t) = volume(t) / factor(t)  (可選)
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

DB_PATH = "回測_0050還原數據.db"


def build_adj_factors(ticker: str, dates: list[str],
                      raw_close: dict[str, float],
                      actions: list[tuple]) -> dict[str, float]:
    """actions: [(ex_date, 'split'|'cash_dividend', ratio, cash_div), ...]
    回傳: {date -> adj_factor}, 最新日 factor=1.0, 越早越小 (分割為正向時)
    """
    factor = {d: 1.0 for d in dates}
    actions_sorted = sorted(actions, key=lambda x: x[0])
    for ex_date, action, ratio, cash_div in actions_sorted:
        if action == "split":
            multiplier = 1.0 / ratio
        elif action == "cash_dividend":
            prev_dates = [d for d in dates if d < ex_date]
            if not prev_dates:
                continue
            p_prev = raw_close.get(prev_dates[-1])
            if not p_prev or p_prev <= 0:
                continue
            multiplier = (p_prev - cash_div) / p_prev
        else:
            continue
        for d in dates:
            if d < ex_date:
                factor[d] *= multiplier
    return factor


def get_adjusted_prices(ticker: str, db_path: str = DB_PATH) -> dict[str, dict]:
    """從 raw + actions 產生還原價"""
    conn = sqlite3.connect(db_path)
    raw = {}
    for date, o, h, lo, c, v in conn.execute(
        "SELECT date, open, high, low, close, volume FROM daily_prices_raw "
        "WHERE ticker=? ORDER BY date", (ticker,)
    ).fetchall():
        raw[date] = {"open": o, "high": h, "low": lo, "close": c, "volume": v}
    actions = conn.execute(
        "SELECT ex_date, action, ratio, cash_dividend FROM corporate_actions "
        "WHERE ticker=? ORDER BY ex_date", (ticker,)
    ).fetchall()
    conn.close()
    dates = sorted(raw.keys())
    raw_close = {d: raw[d]["close"] for d in dates}
    factors = build_adj_factors(ticker, dates, raw_close, actions)
    out = {}
    for d in dates:
        f = factors[d]
        r = raw[d]
        out[d] = {
            "open": r["open"] * f if r["open"] else None,
            "high": r["high"] * f if r["high"] else None,
            "low": r["low"] * f if r["low"] else None,
            "close": r["close"] * f if r["close"] else None,
            "volume": int(r["volume"] / f) if r["volume"] and f > 0 else r["volume"],
            "adj_factor": f,
        }
    return out


if __name__ == "__main__":
    adj = get_adjusted_prices("00631L.TW")
    dates = sorted(adj.keys())
    print("=== 00631L 還原驗證: 分割前後接續性 ===")
    for d in dates:
        if "2026-03-15" <= d <= "2026-04-05":
            r = adj[d]
            print(f"  {d}  close={r['close']:.2f}  (factor={r['adj_factor']:.5f})")
    print()
    print(f"總筆數: {len(dates)}  範圍: {dates[0]} ~ {dates[-1]}")
