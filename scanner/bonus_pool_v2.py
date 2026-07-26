#!/usr/bin/env python3
"""Bonus-pool overlay on H v2.2 — honest capital-timing comparison.

The naive claim "inject cash during a crash -> earn more" is trivially true:
adding money to any positive-expectancy system earns more. That is not edge,
it is just more principal. The ONLY real question a bonus pool answers is:

    Does holding cash on the sidelines and deploying it ONLY on a crash
    beat committing that same capital up-front (or via annual DCA)?

So we fix TOTAL committed capital at t0 across every method and compare
terminal wealth. All decision logic imports from h_strategy (canonical), so
this cannot drift from the live thresholds (VIX>26 / C3 -15%), unlike the
legacy test_bonus_pool.py which was stuck on the old VIX>28 / C3 -10%.

Arms (all commit the same TOTAL capital at t0):
  A  all-in     : entire capital runs v2.2 from day 1
  B  DCA        : base runs v2.2; the rest is fed in as equal annual top-ups
  C  bonus-pool : base runs v2.2; the rest sits as idle reserve, deployed into
                  00631L on a crash trigger and recycled back to cash when the
                  base sleeve exits (disaster/flash sell)

Because total capital and t0 are identical, terminal equity is directly
comparable and the reserve's idle drag is penalized honestly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backtest_core import (
    BacktestConfig, MarketDataLoader, HStrategyInputs, BacktestReporter,
    sma, expanding_max,
)
from h_strategy import (
    StrategyParams, V22, SELL_TAX, FLASH_WINDOW,
    eval_conditions, is_bull, flash_triggered,
    disaster_exit_threshold, reentry_reason, RESET_QUIET_DAYS,
)

SCRIPT_DIR = Path(__file__).parent


@dataclass(frozen=True)
class PoolParams:
    """Bonus-pool trigger config."""
    daily_drop: float = -0.08   # deploy reserve when 00631L single-day close-to-close <= this
    min_conds: int = 3          # ...and disaster n_conds >= this on the trigger day
    redeploy: bool = True       # allow the reserve to fire again after it has recycled to cash


class HPoolBacktester:
    """H v2.2 base sleeve + optional idle reserve deployed on crash triggers.

    base_capital runs pure v2.2. reserve_capital sits as cash until a crash
    trigger fires, then buys 00631L (T+1 open); it is sold back to cash at the
    same time the base sleeve exits. Total capital = base + reserve committed
    at t0, so this is directly comparable to the all-in / DCA arms.
    """

    def __init__(self, config: BacktestConfig, params: StrategyParams | None = None,
                 pool: PoolParams | None = None):
        self.config = config
        self.params = params or V22
        self.pool = pool or PoolParams()

    def run(self, inputs: HStrategyInputs, base_capital: float, reserve_capital: float,
            mode: str = "pool") -> dict:
        """mode: 'pool' (reserve deploys on trigger) | 'allin' (reserve joins base at t0)
        | 'dca' (reserve fed as equal annual top-ups into the base sleeve)."""
        cfg = self.config
        p = self.params
        pp = self.pool
        dates = inputs.dates
        etf631l = inputs.etf631l
        etf0050 = inputs.etf0050

        p0050 = {d: etf0050[d]["close"] for d in dates if d in etf0050 and etf0050[d]["close"]}
        p631l = {d: etf631l[d]["close"] for d in dates if d in etf631l and etf631l[d]["close"]}
        ma60 = sma(p0050, dates, 60)
        ma120 = sma(p0050, dates, 120)
        ma_regime = sma(p0050, dates, p.regime_win)
        exp_max = expanding_max(p631l, dates)
        smh_ma30 = sma(inputs.smh, dates, 30)
        smh_ma60 = sma(inputs.smh, dates, 60)

        prev_close_map: dict[str, float] = {}
        for i, d in enumerate(dates):
            if i > 0 and dates[i - 1] in etf631l and etf631l[dates[i - 1]]["close"]:
                prev_close_map[d] = etf631l[dates[i - 1]]["close"]

        # base sleeve starts all-in; allin mode folds reserve into base at t0
        base_start = base_capital + (reserve_capital if mode == "allin" else 0.0)
        cash = base_start
        shares = 0.0
        position = "out"
        # reserve sleeve
        reserve_cash = reserve_capital if mode in ("pool", "dca") else 0.0
        pool_shares = 0.0
        pool_deployed = False

        # dca schedule: spread reserve across the calendar years present
        years_all = sorted({d[:4] for d in dates
                            if cfg.start_date <= d <= cfg.end_date and d in etf631l})
        dca_years = years_all[1:] if len(years_all) > 1 else years_all
        dca_slice = reserve_capital / len(dca_years) if (mode == "dca" and dca_years) else 0.0

        consec = 0
        pending: tuple[str, str] | None = None
        pool_pending: str | None = None   # "BUY" / "SELL" for the reserve sleeve
        out_low = None
        sell_streak = 0
        quiet_days = 0
        recent_631l: list[float] = []
        last_year = None

        curve: list[dict] = []
        trades: list[dict] = []
        pool_events: list[dict] = []
        total_committed = base_start + reserve_cash  # committed at t0 (reserve is idle but committed)

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

            # ── execute pending base orders (T+1 open) ──
            if pending:
                action, reason = pending
                pending = None
                if action == "BUY":
                    ep = open_ * (1 + cfg.trade_cost)
                    shares = cash / ep
                    cash = 0.0
                    position = "holding"
                    out_low = None
                    trades.append({"date": date, "action": "BUY", "price": round(open_, 2),
                                   "reason": reason})
                elif action == "SELL":
                    ep = open_ * (1 - cfg.trade_cost - SELL_TAX)
                    cash = shares * ep
                    shares = 0.0
                    position = "out"
                    out_low = None
                    trades.append({"date": date, "action": "SELL", "price": round(open_, 2),
                                   "value": round(cash), "reason": reason})

            # ── execute pending pool orders (T+1 open) ──
            if pool_pending:
                if pool_pending == "BUY":
                    ep = open_ * (1 + cfg.trade_cost)
                    pool_shares = reserve_cash / ep
                    reserve_cash = 0.0
                    pool_deployed = True
                    trades.append({"date": date, "action": "POOL_BUY", "price": round(open_, 2),
                                   "reason": "加碼池部署"})
                elif pool_pending == "SELL":
                    ep = open_ * (1 - cfg.trade_cost - SELL_TAX)
                    reserve_cash = pool_shares * ep
                    pool_shares = 0.0
                    pool_deployed = False
                    trades.append({"date": date, "action": "POOL_SELL", "price": round(open_, 2),
                                   "value": round(reserve_cash), "reason": "加碼池回收"})
                pool_pending = None

            # ── DCA top-up (year boundary) into base sleeve ──
            if mode == "dca" and last_year and year != last_year and year in dca_years:
                if dca_slice > 0:
                    reserve_cash -= dca_slice
                    if position == "holding" and close > 0:
                        shares += dca_slice / close
                        trades.append({"date": date, "action": "ADD", "price": round(close, 2),
                                       "shares": round(dca_slice / close, 4), "reason": "DCA年投入"})
                    else:
                        cash += dca_slice
                        trades.append({"date": date, "action": "ADD", "price": round(close, 2),
                                       "shares": 0.0, "reason": "DCA年投入(現金)"})
            last_year = year

            # initial base buy
            if position == "out" and not any(t["action"] == "BUY" for t in trades):
                pending = ("BUY", "初始")

            # ── canonical conditions ──
            c1, c2, c3, c4 = eval_conditions(
                p0050.get(date), ma60.get(date), ma120.get(date),
                inputs.vix.get(date), inputs.vix9d.get(date), inputs.vix3m.get(date),
                close, exp_max.get(date),
                inputs.smh.get(date), smh_ma30.get(date), smh_ma60.get(date),
            )
            n = sum([c1, c2, c3, c4])
            disaster = n >= 3
            consec = consec + 1 if disaster else 0
            bull = is_bull(p0050.get(date), ma_regime.get(date))

            recent_631l.append(close)
            if len(recent_631l) > FLASH_WINDOW:
                recent_631l.pop(0)

            flash_exit = False
            if position == "holding":
                flash_exit, _ = flash_triggered(recent_631l, bull, p)

            threshold = disaster_exit_threshold(sell_streak, bull, p)
            base_exiting = position == "holding" and (consec >= threshold or flash_exit)

            if base_exiting:
                reason = "閃崩防守" if flash_exit else f"disaster(streak={sell_streak})"
                pending = ("SELL", reason)
                sell_streak += 1
                quiet_days = 0
                # recycle the pool sleeve alongside the base exit
                if mode == "pool" and pool_deployed and not pool_pending:
                    pool_pending = "SELL"
            elif position == "out" and not pending:
                if out_low is None or close < out_low:
                    out_low = close
                pc = prev_close_map.get(date)
                reason = reentry_reason(disaster, n, close, pc, close, out_low)
                if reason:
                    pending = ("BUY", reason)

            if position == "holding":
                if n < 3 and not flash_exit:
                    quiet_days += 1
                    if quiet_days >= RESET_QUIET_DAYS:
                        sell_streak = 0
                else:
                    quiet_days = 0

            # ── pool trigger: crash day, deploy idle reserve ──
            if mode == "pool" and not pool_deployed and not pool_pending and reserve_cash > 0:
                pc = prev_close_map.get(date)
                if pc and pc > 0:
                    daily_ret = close / pc - 1
                    if daily_ret <= pp.daily_drop and n >= pp.min_conds:
                        if pp.redeploy or not pool_events:
                            pool_pending = "BUY"
                            pool_events.append({"date": date, "daily_return": round(daily_ret * 100, 2),
                                                "close": round(close, 2), "n_conds": n})

            equity = cash + shares * close + reserve_cash + pool_shares * close
            curve.append({"date": date, "equity": round(equity), "position": position,
                          "n_conds": n, "consec": consec, "sell_streak": sell_streak,
                          "bull": bull, "pool_deployed": pool_deployed})

        return {"curve": curve, "trades": trades, "invested": total_committed,
                "pool_events": pool_events, "_config_initial": total_committed}


def _fmt(m: dict) -> str:
    return (f"終值={m['final']:>6}萬  淨賺={m['net']:>6}萬  CAGR={m['cagr']:>7}  "
            f"MDD={m['mdd']:>7}  Calmar={m['calmar']:>5}")


if __name__ == "__main__":
    cfg = BacktestConfig(initial_capital=1_000_000.0, annual_add=0.0)
    loader = MarketDataLoader(SCRIPT_DIR)
    inputs = loader.load_h_strategy_inputs(cfg, main_ticker="00631L.TW")
    rep = BacktestReporter()

    BASE = 1_000_000.0
    RESERVE = 1_000_000.0   # the capital whose deployment timing we are testing
    TOTAL = BASE + RESERVE

    print("=" * 96)
    print(f"加碼池誠實對照 — 總資本 {TOTAL/1e4:.0f}萬 (base {BASE/1e4:.0f}萬 + 待部署 {RESERVE/1e4:.0f}萬)")
    print("問題：把那 100 萬「留現金等崩盤才進」，有沒有贏過「一開始就投入」或「逐年定額投入」？")
    print("全部 t0 承諾同額資本，含手續費+證交稅，XIRR 口徑一致")
    print("=" * 96)

    eng = HPoolBacktester(cfg, V22)

    allin = eng.run(inputs, BASE, RESERVE, mode="allin")
    dca = eng.run(inputs, BASE, RESERVE, mode="dca")
    for r in (allin, dca):
        r["_config_initial"] = TOTAL

    print(f"  A 全部一開始就投入 (all-in v2.2)   {_fmt(rep.analyze(allin, 'allin'))}")
    print(f"  B 逐年定額投入 (DCA into v2.2)     {_fmt(rep.analyze(dca, 'dca'))}")
    print("-" * 96)
    print("  C 加碼池：留現金，崩盤觸發才部署，base 出場時回收")
    print(f"  {'觸發(單日跌%,災難≥)':22s}  {'終值':>7s}  {'淨賺':>7s}  {'CAGR':>7s}  {'MDD':>8s}  {'Calmar':>6s}  {'觸發次':>5s}")
    print("-" * 96)

    best = None
    for drop in (-0.06, -0.08, -0.10, -0.12):
        for mc in (3, 4):
            pp = PoolParams(daily_drop=drop, min_conds=mc, redeploy=True)
            r = HPoolBacktester(cfg, V22, pp).run(inputs, BASE, RESERVE, mode="pool")
            r["_config_initial"] = TOTAL
            s = rep.analyze(r, f"pool{drop}")
            n_ev = len(r["pool_events"])
            tag = f"單日{drop*100:.0f}% / {mc}條"
            print(f"  {tag:22s}  {s['final']:>6}萬  {s['net']:>6}萬  {s['cagr']:>7}  "
                  f"{s['mdd']:>8}  {s['calmar']:>6}  {n_ev:>5d}")
            fin = float(s['final'])
            if best is None or fin > best[0]:
                best = (fin, tag, s)

    print("=" * 96)
    a = rep.analyze(allin, 'allin')
    print(f"  基準 A 全投入終值 {a['final']}萬。加碼池要贏，C 的終值必須 > {a['final']}萬。")
    if best:
        verdict = "✅ 贏過全投入" if best[0] > float(a['final']) else "❌ 打不過全投入"
        print(f"  加碼池最佳：{best[1]} → 終值 {best[0]:.0f}萬  {verdict}")
    print("=" * 96)
