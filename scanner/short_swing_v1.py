#!/usr/bin/env python3
"""短波段策略 v1 (獨立池, 100萬, 僅做多 00631L)

進場 (T+1 開盤買, 任一成立):
  A 00631L 單日跌幅 <= -4%
  B 00631L 從近 20 日高點回撤 >= -8%
  C VIX 單日飆漲 >= +20%
進場過濾:
  H 災難期 (n_conds >= 3) 禁入
出場 (T+1 開盤賣, 三擇一):
  持有滿 10 個交易日
  報酬 >= +8%
  報酬 <= -5%

資料源:
  00631L.TW / 0050.TW ← 回測_0050還原數據.db (daily_prices)
  VIX / VIX9D / VIX3M / SMH ← scanner/*.json
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "回測_0050還原數據.db"

INITIAL_CAPITAL = 1_000_000
TRADE_COST = 0.001425 * 0.3 + 0.003  # 手續費+交易稅 (買賣合計約略)
# 註: 台股 ETF 交易稅 0.1%, 手續費 0.1425% 可折扣; 這裡保守抓單邊 ~0.0035


def load_prices(ticker: str) -> dict[str, dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT date, open, high, low, close FROM daily_prices WHERE ticker=? ORDER BY date", (ticker,))
    out = {}
    for d, o, h, l, c in cur.fetchall():
        out[d] = {"open": o, "high": h, "low": l, "close": c}
    conn.close()
    return out


def load_json(name: str) -> dict[str, float]:
    return json.load(open(SCRIPT_DIR / f"{name}.json"))


def sma(series: dict[str, float], dates: list[str], n: int) -> dict[str, float]:
    out, buf = {}, []
    for d in dates:
        v = series.get(d)
        if v is None:
            continue
        buf.append(v)
        if len(buf) > n:
            buf.pop(0)
        if len(buf) == n:
            out[d] = sum(buf) / n
    return out


def rolling_high(series: dict[str, float], dates: list[str], n: int) -> dict[str, float]:
    out, buf = {}, []
    for d in dates:
        v = series.get(d)
        if v is None:
            continue
        buf.append(v)
        if len(buf) > n:
            buf.pop(0)
        out[d] = max(buf)
    return out


def expanding_max(series: dict[str, float], dates: list[str]) -> dict[str, float]:
    out, cur = {}, None
    for d in dates:
        v = series.get(d)
        if v is None:
            continue
        cur = v if cur is None else max(cur, v)
        out[d] = cur
    return out


def backtest(start: str, end: str, label: str = ""):
    etf631l = load_prices("00631L.TW")
    etf0050 = load_prices("0050.TW")
    vix = load_json("VIX歷史")
    vix9d = load_json("VIX9D歷史")
    vix3m = load_json("VIX3M歷史")
    smh = load_json("SMH歷史")

    dates = sorted(etf631l.keys())

    p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050}
    p631l = {d: etf631l[d]["close"] for d in dates}
    ma60 = sma(p0050, dates, 60)
    ma120 = sma(p0050, dates, 120)
    smh_ma30 = sma(smh, dates, 30)
    smh_ma60 = sma(smh, dates, 60)
    exp_max_631l = expanding_max(p631l, dates)
    high20 = rolling_high(p631l, dates, 20)

    prev_close_631l = {}
    prev_vix = {}
    for i, d in enumerate(dates):
        if i > 0:
            prev_close_631l[d] = etf631l[dates[i - 1]]["close"]
            pd = dates[i - 1]
            if pd in vix and d in vix:
                prev_vix[d] = vix[pd]

    cash = INITIAL_CAPITAL
    shares = 0.0
    position = "out"
    entry_price = None
    entry_date = None
    hold_days = 0
    pending = None

    curve = []
    trades = []

    def h_disaster(d):
        p50 = p0050.get(d); m60 = ma60.get(d); m120 = ma120.get(d)
        c1 = p50 and m60 and m120 and p50 < m60 and p50 < m120
        v = vix.get(d); v9 = vix9d.get(d); v3 = vix3m.get(d)
        c2 = v and v9 and v3 and v > 28 and v9 > 28 and v3 > 28
        mx = exp_max_631l.get(d); cl = p631l.get(d)
        c3 = mx and cl and (cl / mx - 1) < -0.10
        sm = smh.get(d); s30 = smh_ma30.get(d); s60 = smh_ma60.get(d)
        c4 = sm and s30 and s60 and sm < s30 and sm < s60
        return sum(map(bool, [c1, c2, c3, c4])) >= 3

    for i, d in enumerate(dates):
        if d < start or d > end:
            continue
        if d not in etf631l:
            continue

        o = etf631l[d]["open"]
        c = etf631l[d]["close"]

        # 執行 pending T+1 開盤單
        if pending:
            action, reason = pending
            pending = None
            if action == "BUY":
                ep = o * (1 + TRADE_COST)
                shares = cash / ep
                cash = 0.0
                position = "holding"
                entry_price = ep
                entry_date = d
                hold_days = 0
                trades.append({"date": d, "action": "BUY", "exe_price": round(ep, 2), "reason": reason})
            elif action == "SELL":
                ep = o * (1 - TRADE_COST)
                cash = shares * ep
                ret = (ep / entry_price - 1) * 100 if entry_price else 0
                trades.append({"date": d, "action": "SELL", "exe_price": round(ep, 2),
                               "entry": entry_date, "hold_days": hold_days, "ret_pct": round(ret, 2),
                               "reason": reason})
                shares = 0.0
                position = "out"
                entry_price = None
                entry_date = None
                hold_days = 0

        # 訊號判斷 (收盤後)
        if position == "out":
            if not h_disaster(d):
                pc = prev_close_631l.get(d)
                drop = (c / pc - 1) if pc else None
                h20 = high20.get(d)
                dd20 = (c / h20 - 1) if h20 else None
                pv = prev_vix.get(d)
                vc = vix.get(d)
                vix_jump = (vc / pv - 1) if (pv and vc) else None

                reason = None
                if drop is not None and drop <= -0.04:
                    reason = f"A:單日{drop*100:.1f}%"
                elif dd20 is not None and dd20 <= -0.08:
                    reason = f"B:20日回撤{dd20*100:.1f}%"
                elif vix_jump is not None and vix_jump >= 0.20:
                    reason = f"C:VIX+{vix_jump*100:.1f}%"
                if reason:
                    pending = ("BUY", reason)

        elif position == "holding":
            hold_days += 1
            # 以當日收盤估報酬
            cur_ret = (c / (entry_price / (1 + TRADE_COST)) - 1)  # 以未含成本價估算方向，僅判斷用
            # 更嚴謹: 用收盤對 entry_price (已含進場成本) 比較
            cur_ret = c / entry_price - 1

            reason = None
            if cur_ret >= 0.08:
                reason = f"獲利+{cur_ret*100:.1f}%"
            elif cur_ret <= -0.05:
                reason = f"停損{cur_ret*100:.1f}%"
            elif hold_days >= 10:
                reason = f"時間到({hold_days}日)"
            if reason:
                pending = ("SELL", reason)

        equity = cash + shares * c
        curve.append({"date": d, "equity": equity, "position": position})

    # 強制結算
    if position == "holding" and curve:
        last_d = curve[-1]["date"]
        ep = etf631l[last_d]["close"] * (1 - TRADE_COST)
        cash = shares * ep
        ret = (ep / entry_price - 1) * 100
        trades.append({"date": last_d, "action": "SELL", "exe_price": round(ep, 2),
                       "entry": entry_date, "hold_days": hold_days, "ret_pct": round(ret, 2),
                       "reason": "期末結算"})
        shares = 0.0
        curve[-1]["equity"] = cash

    return {"curve": curve, "trades": trades, "final_cash": cash}


def summary(result, label, start, end):
    curve = result["curve"]
    trades = result["trades"]
    if not curve:
        print(f"{label}: no data"); return
    final = curve[-1]["equity"]
    init = INITIAL_CAPITAL
    ret = (final / init - 1) * 100
    years = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days / 365.25
    cagr = ((final / init) ** (1 / years) - 1) * 100 if years > 0 else 0

    peak = init
    mdd = 0
    for row in curve:
        peak = max(peak, row["equity"])
        dd = (row["equity"] / peak - 1) * 100
        mdd = min(mdd, dd)

    sells = [t for t in trades if t["action"] == "SELL"]
    wins = [t for t in sells if t.get("ret_pct", 0) > 0]
    losses = [t for t in sells if t.get("ret_pct", 0) <= 0]
    avg_ret = sum(t.get("ret_pct", 0) for t in sells) / len(sells) if sells else 0
    avg_hold = sum(t.get("hold_days", 0) for t in sells) / len(sells) if sells else 0
    avg_win = sum(t["ret_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["ret_pct"] for t in losses) / len(losses) if losses else 0
    win_rate = len(wins) / len(sells) * 100 if sells else 0

    print("=" * 70)
    print(f"【{label}】 {start} ~ {end}")
    print("-" * 70)
    print(f"  初始資金         : {init:>12,.0f}")
    print(f"  期末資金         : {final:>12,.0f}")
    print(f"  總報酬           : {ret:>12.2f} %")
    print(f"  CAGR             : {cagr:>12.2f} %")
    print(f"  MDD              : {mdd:>12.2f} %")
    print(f"  交易次數 (完整)  : {len(sells):>12d}")
    print(f"  勝率             : {win_rate:>12.1f} %")
    print(f"  單筆平均報酬     : {avg_ret:>12.2f} %")
    print(f"  平均贏           : {avg_win:>12.2f} %")
    print(f"  平均輸           : {avg_loss:>12.2f} %")
    print(f"  平均持有天數     : {avg_hold:>12.1f}")

    # 出場原因統計
    reason_stats = {}
    for t in sells:
        key = t["reason"].split("(")[0].split("+")[0].split("-")[0].split(":")[0].strip()[:6]
        reason_stats.setdefault(key, []).append(t.get("ret_pct", 0))
    print("  出場原因分佈:")
    for k, rets in reason_stats.items():
        print(f"    {k:10s}  n={len(rets):3d}  avg_ret={sum(rets)/len(rets):+.2f}%")


def print_trades(result, label, limit=None):
    print(f"\n【{label} 交易明細】")
    sells = [t for t in result["trades"] if t["action"] == "SELL"]
    buys = {t["date"]: t for t in result["trades"] if t["action"] == "BUY"}
    for i, s in enumerate(sells):
        if limit and i >= limit:
            print(f"  ... ({len(sells)-limit} more)")
            break
        entry = s.get("entry", "?")
        print(f"  {entry} → {s['date']}  {s.get('hold_days',0):>2}d  "
              f"ret={s.get('ret_pct',0):+6.2f}%  {s.get('reason','')}")


if __name__ == "__main__":
    # 訓練期
    r_train = backtest("2015-01-05", "2024-12-31", "訓練")
    summary(r_train, "訓練期 2015-2024", "2015-01-05", "2024-12-31")
    print_trades(r_train, "訓練期", limit=20)

    # 驗證期
    r_valid = backtest("2025-01-01", "2026-03-20", "驗證")
    summary(r_valid, "驗證期 2025-2026", "2025-01-01", "2026-03-20")
    print_trades(r_valid, "驗證期")
