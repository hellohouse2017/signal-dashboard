#!/usr/bin/env python3
"""H v2.2 optimization research.

Goal: cut bull-market whipsaw (unnecessary exits that re-buy higher) WITHOUT
losing the crash protection that gives H v2.1 its edge.

Observed weakness (measured, not assumed):
  v2.1 re-entry already fires on the first non-disaster day (fast enough).
  The lost upside in bull years (2019/2023/2024) comes from EXITS triggered by
  transient pullbacks -- flash_exit (single -6%) and disaster streaks -- that
  in a confirmed uptrend tend to reverse, so the round trip costs fees and
  re-buys higher.

Levers (both gated by a long-trend regime on 0050 close vs its MA):
  bull_exit_bonus : when 0050 > MA(regime_win), raise disaster exit threshold
                    (2 + sell_streak + bonus). Sensitive in downtrends, patient
                    in confirmed uptrends.
  flash_mode      : how flash-crash defense behaves in a bull regime
                      always     -> current: single -6% or 6-bar -15%
                      bull_relax -> in bull require -9% / -22%
                      bull_off   -> disable flash while bull

Correctness fixes vs backtest_core:
  - sell now includes 0.1% securities transaction tax (backtest_core omits it,
    which slightly overstates every H-strategy result). All numbers here are
    net of fee + tax so baseline and variants are compared on the same ruler.

Validation:
  train  2015-2020  (parameters chosen here)
  test   2021-2026  (out-of-sample; never used to pick parameters)
  A variant is only acceptable if it holds up out-of-sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backtest_core import (
    BacktestConfig,
    MarketDataLoader,
    HStrategyBacktester,
    sma,
    expanding_max,
)

SCRIPT_DIR = Path(__file__).parent
RESET_QUIET_DAYS = 30
SELL_TAX = 0.001  # securities transaction tax on sell (missing in backtest_core)


@dataclass(frozen=True)
class StrategyParams:
    regime_win: int = 200          # long-trend MA window on 0050
    bull_exit_bonus: int = 0       # extra consecutive-disaster days required while bull
    flash_mode: str = "always"     # always | bull_relax | bull_off


def run_h(inputs, cfg: BacktestConfig, p: StrategyParams) -> dict:
    """Parameterized H strategy. p == StrategyParams() reproduces v2.1 (plus sell tax)."""
    dates = inputs.dates
    etf631l = inputs.etf631l
    etf0050 = inputs.etf0050

    p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050 and etf0050[d]["close"]}
    p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l and etf631l[d]["close"]}
    ma60_0050 = sma(p0050, dates, 60)
    ma120_0050 = sma(p0050, dates, 120)
    ma_regime = sma(p0050, dates, p.regime_win)
    exp_max = expanding_max(p631l, dates)
    smh_ma30 = sma(inputs.smh, dates, 30)
    smh_ma60 = sma(inputs.smh, dates, 60)

    prev_close_map: dict[str, float] = {}
    for i, d in enumerate(dates):
        if i > 0 and dates[i - 1] in etf631l and etf631l[dates[i - 1]]["close"]:
            prev_close_map[d] = etf631l[dates[i - 1]]["close"]

    cash = cfg.initial_capital
    shares = 0.0
    position = "out"
    invested = cfg.initial_capital
    last_year = None
    consec = 0
    pending = None
    out_low = None
    out_low_date = None
    sell_streak = 0
    quiet_days = 0
    recent_631l: list[float] = []

    curve: list[dict] = []
    trades: list[dict] = []

    def buy(exe_date, price, reason):
        nonlocal cash, shares, position, out_low, out_low_date
        ep = price * (1 + cfg.trade_cost)
        shares = cash / ep
        cash = 0.0
        position = "holding"
        out_low = None
        out_low_date = None
        trades.append({"date": exe_date, "action": "BUY", "exe_price": round(ep, 4),
                       "reason": reason})

    def sell(exe_date, price, reason):
        nonlocal cash, shares, position, out_low, out_low_date
        ep = price * (1 - cfg.trade_cost - SELL_TAX)
        cash = shares * ep
        shares = 0.0
        position = "out"
        out_low = None
        out_low_date = None
        trades.append({"date": exe_date, "action": "SELL", "exe_price": round(ep, 4),
                       "value": round(cash), "reason": reason})

    for date in dates:
        if date < cfg.start_date or date > cfg.end_date:
            continue
        if date not in etf631l:
            continue
        row = etf631l[date]
        close = row["close"]
        open_ = row["open"]
        if not close or not open_:
            continue
        year = date[:4]

        if pending:
            action = pending[0]
            pending = None
            if action == "BUY":
                buy(date, open_, "T+1回場")
            elif action == "SELL":
                sell(date, open_, pending_reason)

        if last_year and year != last_year and int(year) >= 2016 and cfg.annual_add > 0:
            topup = cfg.annual_add
            cash += topup
            invested += topup
            if position == "holding" and close > 0:
                ns = topup / close
                shares += ns
                cash -= topup
        last_year = year

        if position == "out" and not trades:
            pending = ("BUY", date)
            pending_reason = "初始"

        p50 = p0050.get(date); ma60 = ma60_0050.get(date); ma120 = ma120_0050.get(date)
        c1 = p50 is not None and ma60 and ma120 and p50 < ma60 and p50 < ma120
        v = inputs.vix.get(date); v9 = inputs.vix9d.get(date); v3 = inputs.vix3m.get(date)
        c2 = v and v9 and v3 and v > 26 and v9 > 26 and v3 > 26
        mx = exp_max.get(date)
        c3 = (close / mx - 1) < -0.15 if mx else False
        smh = inputs.smh.get(date); s30 = smh_ma30.get(date); s60 = smh_ma60.get(date)
        c4 = smh and s30 and s60 and smh < s30 and smh < s60
        n = sum([bool(c1), bool(c2), bool(c3), bool(c4)])
        disaster = n >= 3
        consec = consec + 1 if disaster else 0

        # regime: is 0050 in a confirmed long uptrend?
        rma = ma_regime.get(date)
        bull = bool(p50 is not None and rma and p50 > rma)

        recent_631l.append(close)
        if len(recent_631l) > 6:
            recent_631l.pop(0)

        # flash-crash defense (regime-aware)
        flash_exit = False
        if position == "holding":
            if p.flash_mode == "bull_off" and bull:
                day_thr, win_thr = None, None
            elif p.flash_mode == "bull_relax" and bull:
                day_thr, win_thr = -0.09, -0.22
            else:
                day_thr, win_thr = -0.06, -0.15
            if day_thr is not None:
                pc = prev_close_map.get(date)
                if pc and pc > 0 and (close / pc - 1) <= day_thr:
                    flash_exit = True
                if not flash_exit and len(recent_631l) == 6 and (close / recent_631l[0] - 1) <= win_thr:
                    flash_exit = True

        bonus = p.bull_exit_bonus if bull else 0
        sell_threshold = 2 + sell_streak + bonus

        if position == "holding" and (consec >= sell_threshold or flash_exit):
            pending = ("SELL", date)
            pending_reason = "閃崩防守" if flash_exit else f"disaster(streak={sell_streak})"
            sell_streak += 1
            quiet_days = 0
        elif position == "out" and not pending:
            if out_low is None or close < out_low:
                out_low = close
                out_low_date = date
            reason = None
            if not disaster:
                reason = f"回場({n}/4)"
            pc = prev_close_map.get(date)
            if not reason and pc and pc > 0 and close / pc - 1 >= 0.08:
                reason = "單日+8%"
            if not reason and out_low and close / out_low - 1 >= 0.20:
                reason = "反彈+20%"
            if reason:
                pending = ("BUY", date)
                pending_reason = reason

        if position == "holding":
            if n < 3 and not flash_exit:
                quiet_days += 1
                if quiet_days >= RESET_QUIET_DAYS:
                    sell_streak = 0
            else:
                quiet_days = 0

        equity = cash + shares * close
        curve.append({"date": date, "equity": round(equity), "position": position})

    return {"curve": curve, "trades": trades, "invested": invested}


def metrics(result: dict) -> dict:
    curve = result["curve"]
    if not curve:
        return {}
    eq = [r["equity"] for r in curve]
    initial = eq[0]
    final = eq[-1]
    d0 = datetime.strptime(curve[0]["date"], "%Y-%m-%d")
    d1 = datetime.strptime(curve[-1]["date"], "%Y-%m-%d")
    years = (d1 - d0).days / 365.25
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and initial > 0 else 0.0
    peak = eq[0]; mdd = 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    calmar = cagr / abs(mdd) if mdd else 0.0
    sells = sum(1 for t in result["trades"] if t["action"] == "SELL")
    return {"final": round(final / 1e4), "cagr": cagr, "mdd": mdd,
            "calmar": calmar, "sells": sells}


def run_period(inputs, start, end, p: StrategyParams) -> dict:
    cfg = BacktestConfig(start_date=start, end_date=end,
                         initial_capital=1_000_000.0, annual_add=0.0)
    return metrics(run_h(inputs, cfg, p))


def fmt(m: dict) -> str:
    return (f"final={m['final']:>6}萬  cagr={m['cagr']*100:>6.1f}%  "
            f"mdd={m['mdd']*100:>6.1f}%  calmar={m['calmar']:>4.2f}  sells={m['sells']:>2}")


def main() -> None:
    loader = MarketDataLoader(SCRIPT_DIR)
    cfg = BacktestConfig(initial_capital=1_000_000.0, annual_add=0.0)
    inputs = loader.load_h_strategy_inputs(cfg, main_ticker="00631L.TW")

    TRAIN = ("2015-01-01", "2020-12-31")
    TEST = ("2021-01-01", "2099-12-31")

    baseline = StrategyParams()  # == v2.1 (+ sell tax)

    grid = []
    for win in (150, 200):
        for bonus in (0, 1, 2, 3):
            for mode in ("always", "bull_relax", "bull_off"):
                grid.append(StrategyParams(regime_win=win, bull_exit_bonus=bonus, flash_mode=mode))

    print("=" * 100)
    print("BASELINE  H v2.1 (+sell tax)   [same 100萬, annual_add=0]")
    print("-" * 100)
    b_tr = run_period(inputs, *TRAIN, baseline)
    b_te = run_period(inputs, *TEST, baseline)
    print(f"  TRAIN 2015-2020  {fmt(b_tr)}")
    print(f"  TEST  2021-2026  {fmt(b_te)}")

    print("\n" + "=" * 100)
    print("GRID  (train picks params; test is out-of-sample)")
    print("-" * 100)
    print(f"{'win':>4} {'bonus':>5} {'flash':>11} | "
          f"{'TR cagr':>8} {'TR mdd':>8} {'TR clmr':>7} | "
          f"{'TE cagr':>8} {'TE mdd':>8} {'TE clmr':>7} {'TE sells':>8}")
    print("-" * 100)

    rows = []
    for p in grid:
        tr = run_period(inputs, *TRAIN, p)
        te = run_period(inputs, *TEST, p)
        rows.append((p, tr, te))
        print(f"{p.regime_win:>4} {p.bull_exit_bonus:>5} {p.flash_mode:>11} | "
              f"{tr['cagr']*100:>7.1f}% {tr['mdd']*100:>7.1f}% {tr['calmar']:>7.2f} | "
              f"{te['cagr']*100:>7.1f}% {te['mdd']*100:>7.1f}% {te['calmar']:>7.2f} {te['sells']:>8}")

    # Selection rule (decided on TRAIN only, then read TEST honestly):
    #   keep candidates whose TRAIN Calmar >= baseline TRAIN Calmar
    #   and TRAIN mdd not worse than baseline;  rank by TRAIN Calmar.
    keep = [(p, tr, te) for (p, tr, te) in rows
            if tr["calmar"] >= b_tr["calmar"] and tr["mdd"] >= b_tr["mdd"] - 0.005]
    keep.sort(key=lambda r: (r[1]["calmar"], r[1]["cagr"]), reverse=True)

    print("\n" + "=" * 100)
    print("TRAIN-selected candidates (ranked by TRAIN Calmar), with honest OOS TEST:")
    print("-" * 100)
    for p, tr, te in keep[:6]:
        print(f"  win={p.regime_win} bonus={p.bull_exit_bonus} flash={p.flash_mode}")
        print(f"     TRAIN {fmt(tr)}")
        print(f"     TEST  {fmt(te)}   (baseline TEST cagr={b_te['cagr']*100:.1f}% "
              f"mdd={b_te['mdd']*100:.1f}% calmar={b_te['calmar']:.2f})")

    if keep:
        best = keep[0]
        p, tr, te = best
        print("\n" + "=" * 100)
        print("VERDICT")
        print("-" * 100)
        print(f"  Best by TRAIN Calmar: win={p.regime_win} bonus={p.bull_exit_bonus} flash={p.flash_mode}")
        d_cagr = (te["cagr"] - b_te["cagr"]) * 100
        d_mdd = (te["mdd"] - b_te["mdd"]) * 100
        d_clmr = te["calmar"] - b_te["calmar"]
        print(f"  OOS TEST vs baseline:  ΔCAGR={d_cagr:+.1f}pp  ΔMDD={d_mdd:+.1f}pp  ΔCalmar={d_clmr:+.2f}")
        if d_cagr > 0 and te["mdd"] >= b_te["mdd"] - 0.01:
            print("  -> Improves OOS return without materially worse drawdown. WORTH ADOPTING.")
        elif d_clmr > 0:
            print("  -> Better OOS risk-adjusted (Calmar) but check the CAGR/MDD tradeoff.")
        else:
            print("  -> Does NOT beat baseline out-of-sample. Keep v2.1 as is.")


if __name__ == "__main__":
    main()
