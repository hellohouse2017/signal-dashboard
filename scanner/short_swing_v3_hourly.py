#!/usr/bin/env python3
"""短波段策略 v3 (1h 時線版, 獨立池, 100萬, 僅做多 00631L)

相對 v1 (日線):
  週期從日線 -> 1h
  訊號門檻縮小 (1h 波動較日小)
  持有上限以 "TW 交易日數" 計算, 而非 bar 數

進場 (下一根 1h 開盤買, 任一成立):
  A 00631L 單根 1h 跌幅 <= -2%
  B 00631L 從近 40 根 1h 高點回撤 >= -5%   (~1 週)
  C VIX 單日 (日級) 飆漲 >= +15%           (用日級避免 1h 訊號過噪)

進場過濾:
  H 災難期 (日線 n_conds >= 3) 禁入

出場 (下一根 1h 開盤賣, 三擇一):
  持有滿 10 個 TW 交易日 (依日期計算)
  報酬 >= +8%
  報酬 <= -5%

資料源:
  scanner/intraday.db hourly_bars (00631L.TW / ^VIX)
  scanner/回測_0050還原數據.db daily_prices (0050 做災難期)
  scanner/*.json (VIX/VIX9D/VIX3M/SMH 日級)
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
DAILY_DB = SCRIPT_DIR / "回測_0050還原數據.db"
INTRA_DB = SCRIPT_DIR / "intraday.db"

INITIAL_CAPITAL = 1_000_000
TRADE_COST = 0.001425 * 0.3 + 0.003

# 訊號門檻
BAR_DROP_TH = -0.02          # A: 單根 1h <= -2%
ROLL_HIGH_BARS = 40          # B: rolling high 回看 bar 數
DD_TH = -0.05                # B: 回撤閾值
VIX_JUMP_TH = 0.15           # C: VIX 日漲 >= +15%

# 出場門檻
MAX_HOLD_DAYS = 10
TP_TH = 0.08
SL_TH = -0.05


def load_daily_prices(ticker: str) -> dict[str, dict]:
    conn = sqlite3.connect(DAILY_DB)
    cur = conn.execute(
        "SELECT date, open, high, low, close FROM daily_prices WHERE ticker=? ORDER BY date",
        (ticker,),
    )
    out = {d: {"open": o, "high": h, "low": l, "close": c} for d, o, h, l, c in cur}
    conn.close()
    return out


def load_hourly(ticker: str) -> list[dict]:
    """載入 1h 時線並套用還原因子 (以 corporate_actions 為準).

    yfinance auto_adjust=True 對 intraday 不生效, 需自行以 ex_date
    為界套 split/cash_dividend 因子; 最新日 factor=1.0, 越早越小.
    """
    conn = sqlite3.connect(INTRA_DB)
    cur = conn.execute(
        "SELECT datetime, open, high, low, close FROM hourly_bars WHERE ticker=? ORDER BY datetime",
        (ticker,),
    )
    rows = [{"dt": dt, "open": o, "high": h, "low": l, "close": c}
            for dt, o, h, l, c in cur]
    conn.close()
    if not rows:
        return rows

    # 載入 corporate_actions (daily DB 內)
    try:
        dconn = sqlite3.connect(DAILY_DB)
        actions = dconn.execute(
            "SELECT ex_date, action, ratio, cash_dividend FROM corporate_actions "
            "WHERE ticker=? ORDER BY ex_date", (ticker,)
        ).fetchall()
        dconn.close()
    except sqlite3.OperationalError:
        actions = []

    if not actions:
        return rows

    # 為了 cash_dividend 計算 p_prev, 需要最接近 ex_date 前一日的 close
    # intraday 用該日最後一根 bar 的 close 近似
    day_last_close: dict[str, float] = {}
    for r in rows:
        day_last_close[r["dt"][:10]] = r["close"]
    sorted_days = sorted(day_last_close.keys())

    # 逐 bar 累乘 factor (對每個 ex_date, bar.date < ex_date 才套)
    factors = [1.0] * len(rows)
    for ex_date, action, ratio, cash_div in actions:
        if action == "split" and ratio:
            mult = 1.0 / ratio
        elif action == "cash_dividend" and cash_div:
            prev_days = [d for d in sorted_days if d < ex_date]
            if not prev_days:
                continue
            p_prev = day_last_close.get(prev_days[-1])
            if not p_prev or p_prev <= 0:
                continue
            mult = (p_prev - cash_div) / p_prev
        else:
            continue
        for i, r in enumerate(rows):
            if r["dt"][:10] < ex_date:
                factors[i] *= mult

    for i, r in enumerate(rows):
        f = factors[i]
        r["open"] *= f
        r["high"] *= f
        r["low"] *= f
        r["close"] *= f
    return rows


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


def expanding_max(series: dict[str, float], dates: list[str]) -> dict[str, float]:
    out, cur = {}, None
    for d in dates:
        v = series.get(d)
        if v is None:
            continue
        cur = v if cur is None else max(cur, v)
        out[d] = cur
    return out


def build_daily_disaster(dates: list[str]) -> dict[str, bool]:
    """建立每日是否處災難期 (n_conds >= 3) 的 map."""
    etf0050 = load_daily_prices("0050.TW")
    etf631l = load_daily_prices("00631L.TW")
    vix = load_json("VIX歷史")
    vix9d = load_json("VIX9D歷史")
    vix3m = load_json("VIX3M歷史")
    smh = load_json("SMH歷史")

    p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050}
    p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l}
    ma60 = sma(p0050, dates, 60)
    ma120 = sma(p0050, dates, 120)
    smh30 = sma(smh, dates, 30)
    smh60 = sma(smh, dates, 60)
    exp_max = expanding_max(p631l, dates)

    out = {}
    for d in dates:
        p50 = p0050.get(d); m60 = ma60.get(d); m120 = ma120.get(d)
        c1 = p50 and m60 and m120 and p50 < m60 and p50 < m120
        v = vix.get(d); v9 = vix9d.get(d); v3 = vix3m.get(d)
        c2 = v and v9 and v3 and v > 28 and v9 > 28 and v3 > 28
        mx = exp_max.get(d); cl = p631l.get(d)
        c3 = mx and cl and (cl / mx - 1) < -0.10
        sm = smh.get(d); s30 = smh30.get(d); s60 = smh60.get(d)
        c4 = sm and s30 and s60 and sm < s30 and sm < s60
        out[d] = sum(map(bool, [c1, c2, c3, c4])) >= 3
    return out


def build_vix_jump_daily() -> dict[str, float]:
    """日級 VIX 漲幅 (vs 前一交易日)."""
    vix = load_json("VIX歷史")
    dates = sorted(vix.keys())
    out = {}
    for i in range(1, len(dates)):
        pv = vix[dates[i - 1]]
        cv = vix[dates[i]]
        if pv and cv:
            out[dates[i]] = cv / pv - 1
    return out


def backtest(start: str, end: str, label: str = ""):
    bars = load_hourly("00631L.TW")
    bars = [b for b in bars if start <= b["dt"][:10] <= end]
    if not bars:
        return {"curve": [], "trades": [], "final_cash": INITIAL_CAPITAL}

    all_dates = sorted({b["dt"][:10] for b in bars})

    # 日線災難期 (用 daily DB 的完整日期序列, 才能算 MA)
    conn = sqlite3.connect(DAILY_DB)
    cur = conn.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date")
    full_dates = [r[0] for r in cur]
    conn.close()
    disaster = build_daily_disaster(full_dates)
    vix_jump = build_vix_jump_daily()

    cash = INITIAL_CAPITAL
    shares = 0.0
    position = "out"
    entry_price = None
    entry_date = None
    entry_bar_idx = None
    pending = None
    curve = []
    trades = []

    roll_high = []  # close buffer for rolling 40-bar high

    for i, b in enumerate(bars):
        d = b["dt"][:10]
        o = b["open"]; c = b["close"]

        # 執行 pending (下一根 1h 開盤)
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
                entry_bar_idx = i
                trades.append({"dt": b["dt"], "action": "BUY",
                               "exe_price": round(ep, 2), "reason": reason})
            elif action == "SELL":
                ep = o * (1 - TRADE_COST)
                cash = shares * ep
                ret = (ep / entry_price - 1) * 100 if entry_price else 0
                hold_days = len({bb["dt"][:10] for bb in bars[entry_bar_idx:i]})
                trades.append({"dt": b["dt"], "action": "SELL",
                               "exe_price": round(ep, 2),
                               "entry": entry_date, "hold_days": hold_days,
                               "ret_pct": round(ret, 2), "reason": reason})
                shares = 0.0
                position = "out"
                entry_price = None
                entry_date = None
                entry_bar_idx = None

        # 更新 rolling high buffer
        roll_high.append(c)
        if len(roll_high) > ROLL_HIGH_BARS:
            roll_high.pop(0)
        cur_hi = max(roll_high) if roll_high else c

        if position == "out":
            if not disaster.get(d, False):
                prev_c = bars[i - 1]["close"] if i > 0 else None
                bar_drop = (c / prev_c - 1) if prev_c else None
                dd = (c / cur_hi - 1) if cur_hi else None
                vj = vix_jump.get(d)

                reason = None
                if bar_drop is not None and bar_drop <= BAR_DROP_TH:
                    reason = f"A:1h{bar_drop*100:.1f}%"
                elif dd is not None and dd <= DD_TH and len(roll_high) >= ROLL_HIGH_BARS:
                    reason = f"B:回撤{dd*100:.1f}%"
                elif vj is not None and vj >= VIX_JUMP_TH:
                    reason = f"C:VIX+{vj*100:.1f}%"
                if reason:
                    pending = ("BUY", reason)

        elif position == "holding":
            cur_ret = c / entry_price - 1
            hold_days = len({bb["dt"][:10] for bb in bars[entry_bar_idx:i + 1]})
            reason = None
            if cur_ret >= TP_TH:
                reason = f"獲利+{cur_ret*100:.1f}%"
            elif cur_ret <= SL_TH:
                reason = f"停損{cur_ret*100:.1f}%"
            elif hold_days >= MAX_HOLD_DAYS:
                reason = f"時間到({hold_days}日)"
            if reason:
                pending = ("SELL", reason)

        equity = cash + shares * c
        curve.append({"dt": b["dt"], "equity": equity, "position": position})

    # 強制結算
    if position == "holding" and curve:
        last = bars[-1]
        ep = last["close"] * (1 - TRADE_COST)
        cash = shares * ep
        ret = (ep / entry_price - 1) * 100
        hold_days = len({bb["dt"][:10] for bb in bars[entry_bar_idx:]})
        trades.append({"dt": last["dt"], "action": "SELL",
                       "exe_price": round(ep, 2), "entry": entry_date,
                       "hold_days": hold_days, "ret_pct": round(ret, 2),
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

    reason_stats = {}
    for t in sells:
        key = t["reason"].split("(")[0].split("+")[0].split("-")[0].split(":")[0].strip()[:6]
        reason_stats.setdefault(key, []).append(t.get("ret_pct", 0))
    print("  出場原因分佈:")
    for k, rets in reason_stats.items():
        print(f"    {k:10s}  n={len(rets):3d}  avg_ret={sum(rets)/len(rets):+.2f}%")

    entry_stats = {}
    for t in trades:
        if t["action"] != "BUY":
            continue
        k = t["reason"].split(":")[0]
        entry_stats.setdefault(k, 0)
        entry_stats[k] += 1
    print("  進場訊號分佈:")
    for k, n in entry_stats.items():
        print(f"    {k:10s}  n={n:3d}")


def print_trades(result, label, limit=None):
    print(f"\n【{label} 交易明細】")
    sells = [t for t in result["trades"] if t["action"] == "SELL"]
    for i, s in enumerate(sells):
        if limit and i >= limit:
            print(f"  ... ({len(sells)-limit} more)")
            break
        entry = s.get("entry", "?")
        print(f"  {entry} → {s['dt']}  {s.get('hold_days',0):>2}d  "
              f"ret={s.get('ret_pct',0):+6.2f}%  {s.get('reason','')}")


if __name__ == "__main__":
    r_train = backtest("2023-07-04", "2025-06-30", "訓練")
    summary(r_train, "訓練期 2023-07~2025-06", "2023-07-04", "2025-06-30")
    print_trades(r_train, "訓練期", limit=30)

    r_valid = backtest("2025-07-01", "2026-04-17", "驗證")
    summary(r_valid, "驗證期 2025-07~2026-04", "2025-07-01", "2026-04-17")
    print_trades(r_valid, "驗證期")
