#!/usr/bin/env python3
"""H strategy canonical definition — single source of truth (v2.2).

This module IS the definition of the H strategy. Both the backtester
(`h_v2_1.py`) and the live notifier (`signal_notify.py`) import their
condition/decision logic from here, so their behavior can never drift.

Before this module existed the strategy loop was copy-pasted into 4+ files
with inconsistent thresholds (VIX >26 vs >28, C3 -10% vs -15%). That is now
fixed: change a rule here and every consumer follows.

v2.2 vs v2.1 (validated out-of-sample 2021-2026, see optimize_h_v2_2.py):
  - bull regime filter: while 0050 close > its MA200, raise the disaster
    exit threshold by `bull_exit_bonus` (patient in confirmed uptrends,
    sensitive in downtrends) -> cuts whipsaw exits that re-buy higher.
  - flash defense relaxes in a bull regime (`bull_relax`): single -9% /
    6-bar -22% instead of -6% / -15%.
  Out-of-sample result on 100萬, annual_add=0, net of fee + sell tax
  (as of 2026-07-06 data; point estimates drift with new data — treat as ranges):
    CAGR 72.9% -> 75.7% | MDD -32.3% -> -30.7% | Calmar 2.26 -> 2.46 | sells 24 -> 15

To reproduce legacy v1/v2.1 behavior for comparison:
    StrategyParams(bull_exit_bonus=0, flash_mode="always")
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Canonical thresholds (the ONLY place these live) ───────────────────────────
VIX_THRESHOLD = 26.0        # C2: VIX / VIX9D / VIX3M all above this
C3_DRAWDOWN = -0.15         # C3: 00631L adjusted drawdown from expanding high
RESET_QUIET_DAYS = 30       # holding + n_conds<3 this many days -> reset sell_streak

TRADE_COST = 0.001425       # brokerage fee, single side
SELL_TAX = 0.001            # securities transaction tax, sell side only

# Flash-crash defense thresholds
FLASH_DAY = -0.06           # single-day close-to-close
FLASH_WIN = -0.15           # cumulative over FLASH_WINDOW closes
FLASH_DAY_BULL = -0.09      # relaxed while bull
FLASH_WIN_BULL = -0.22
FLASH_WINDOW = 6            # number of closes (today + 5 prior = 5-day span)

# v2.2 regime defaults
REGIME_WIN = 200            # long-trend MA window on 0050 close
BULL_EXIT_BONUS = 1         # extra consecutive-disaster days required while bull
FLASH_MODE = "bull_relax"   # always | bull_relax | bull_off


@dataclass(frozen=True)
class StrategyParams:
    """H strategy parameters. Defaults ARE the live v2.2 configuration."""
    regime_win: int = REGIME_WIN
    bull_exit_bonus: int = BULL_EXIT_BONUS
    flash_mode: str = FLASH_MODE  # always | bull_relax | bull_off


# Named presets ------------------------------------------------------------------
V22 = StrategyParams()                                              # live
V21 = StrategyParams(bull_exit_bonus=0, flash_mode="always")       # legacy reference


# ── Pure decision helpers (shared by backtest AND live notifier) ───────────────

def asof_date_map(series: dict[str, float], dates: list[str]) -> dict[str, str | None]:
    """For each date in `dates`, the latest key of `series` on or BEFORE it.

    Canonical alignment of US series (VIX/SMH) to TW trading dates: on a US
    holiday the last available US close still counts, matching the live
    notifier's forward-fill. Backtests must use this instead of same-date
    lookup, which silently forces C2/C4 false on US holidays.
    `dates` must be sorted ascending and contain all series keys of interest.
    """
    out: dict[str, str | None] = {}
    last: str | None = None
    for d in dates:
        if d in series:
            last = d
        out[d] = last
    return out


def eval_conditions(
    p50: float | None, ma60: float | None, ma120: float | None,
    vix: float | None, vix9d: float | None, vix3m: float | None,
    close_adj: float | None, expmax_adj: float | None,
    smh: float | None, smh30: float | None, smh60: float | None,
) -> tuple[bool, bool, bool, bool]:
    """Evaluate the 4 disaster conditions. Returns (c1, c2, c3, c4)."""
    c1 = bool(p50 and ma60 and ma120 and p50 < ma60 and p50 < ma120)
    c2 = bool(vix and vix9d and vix3m
              and vix > VIX_THRESHOLD and vix9d > VIX_THRESHOLD and vix3m > VIX_THRESHOLD)
    c3 = bool(close_adj and expmax_adj and expmax_adj > 0
              and (close_adj / expmax_adj - 1) < C3_DRAWDOWN)
    c4 = bool(smh and smh30 and smh60 and smh < smh30 and smh < smh60)
    return c1, c2, c3, c4


def is_bull(p50: float | None, ma_regime: float | None) -> bool:
    """True when 0050 close is above its long-trend MA (confirmed uptrend)."""
    return bool(p50 is not None and ma_regime and p50 > ma_regime)


def flash_thresholds(bull: bool, params: StrategyParams) -> tuple[float | None, float | None]:
    """Return (day_threshold, window_threshold); (None, None) means disabled."""
    if params.flash_mode == "bull_off" and bull:
        return None, None
    if params.flash_mode == "bull_relax" and bull:
        return FLASH_DAY_BULL, FLASH_WIN_BULL
    return FLASH_DAY, FLASH_WIN


def flash_triggered(
    recent_closes: list[float], bull: bool, params: StrategyParams,
) -> tuple[bool, str]:
    """Flash-crash defense. `recent_closes` are consecutive trading-day closes
    ending with today (max FLASH_WINDOW long). Returns (triggered, detail)."""
    day_thr, win_thr = flash_thresholds(bull, params)
    if day_thr is None:
        return False, "多頭放寬:關閉"
    if len(recent_closes) >= 2:
        day_ret = recent_closes[-1] / recent_closes[-2] - 1
        if day_ret <= day_thr:
            return True, f"單日跌幅 {day_ret * 100:.1f}%"
    if len(recent_closes) == FLASH_WINDOW:
        win_ret = recent_closes[-1] / recent_closes[0] - 1
        if win_ret <= win_thr:
            return True, f"5日累積跌幅 {win_ret * 100:.1f}%"
    return False, "未觸發"


def disaster_exit_threshold(sell_streak: int, bull: bool, params: StrategyParams) -> int:
    """Consecutive-disaster days required to exit: 2 + streak (+ bonus while bull)."""
    return 2 + sell_streak + (params.bull_exit_bonus if bull else 0)


def reentry_reason(
    disaster: bool, n_conds: int,
    close_adj: float | None, prev_close_adj: float | None,
    close_raw: float | None, out_low: float | None,
) -> str | None:
    """Re-entry (three-of-one). Returns a reason string or None."""
    if not disaster:
        return f"非災難回場（{n_conds}/4）"
    if close_adj and prev_close_adj and prev_close_adj > 0:
        daily_gain = close_adj / prev_close_adj - 1
        if daily_gain >= 0.08:
            return f"急漲回場（+{daily_gain * 100:.1f}%）"
    if close_raw and out_low and out_low > 0:
        bounce = close_raw / out_low - 1
        if bounce >= 0.20:
            return f"反彈回場（+{bounce * 100:.1f}%）"
    return None
