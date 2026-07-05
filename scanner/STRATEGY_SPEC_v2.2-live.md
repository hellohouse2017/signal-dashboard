# H Strategy v2.2 Live Spec

Last updated: 2026-07-06

This is the live source-of-truth spec for the `00631L` main-position system.
Behavior is defined once in code at `scanner/h_strategy.py` (the canonical module),
and imported by both the backtester (`h_v2_2.py`) and the daily notifier
(`signal_notify.py`). If any doc and the canonical module disagree, the module wins.

Supersedes `STRATEGY_SPEC_v2.1-live.md` (now legacy).

---

## 1. Scope

- Instrument: `00631L.TW`
- Role: main position, all-in / all-out
- Execution: T+1 at next session open
- Data basis:
  - `00631L.TW` and `0050.TW`: adjusted OHLCV via `adjuster.py`
  - `VIX`, `VIX9D`, `VIX3M`, `SMH`: JSON daily series

---

## 2. Core idea

Do not sell on ordinary pullbacks. Exit only when market danger is confirmed by
the disaster framework, or when a flash-crash defense triggers. v2.2 adds a
long-trend regime filter so the system is **patient in confirmed uptrends** and
**sensitive in downtrends**, which cuts whipsaw exits that would otherwise re-buy
higher.

---

## 3. Disaster conditions

Evaluated each day (thresholds live in `h_strategy.py`):

| Condition | Logic |
|---|---|
| C1 | `0050 close < MA60` and `< MA120` |
| C2 | `VIX > 26` and `VIX9D > 26` and `VIX3M > 26` |
| C3 | `00631L` adjusted-close drawdown from expanding high `< -15%` |
| C4 | `SMH close < MA30` and `< MA60` |

`n_conds = number triggered`; `disaster = (n_conds >= 3)`.

---

## 4. Regime filter (new in v2.2)

`bull = 0050 close > MA200` (window `REGIME_WIN`, 0050 close).

While `bull`:
- disaster exit threshold gets `+bull_exit_bonus` (default `+1`)
- flash defense relaxes (`bull_relax`): single `-9%` / 6-bar `-22%`
  instead of the downtrend `-6%` / `-15%`

While not `bull`: identical to v2.1 sensitivity.

---

## 5. Exit logic

### 5.1 Progressive disaster exit

```text
sell_threshold = 2 + sell_streak + (bull_exit_bonus if bull else 0)
```

- 1st exit in a noisy regime: `2` consecutive disaster days (`+1` if bull)
- each subsequent exit in the same unreset streak: `+1` more
- mark pending sell on trigger day, execute at next open (T+1)

### 5.2 Flash-crash defense (regime-aware)

Trigger immediate pending sell while holding if either:
- single-day close-to-close `<= day_threshold`
- 6-close cumulative `<= window_threshold`

thresholds: `-6% / -15%` normally, `-9% / -22%` while bull. Execution T+1.

### 5.3 Quiet reset

While holding, if `n_conds < 3` for `30` consecutive trading days → `sell_streak = 0`.

---

## 6. Re-entry logic

While out, re-enter at next open if any one holds:

| Re-entry | Logic |
|---|---|
| A | not a disaster day (`n_conds < 3`) |
| B | `00631L` adjusted close rises `>= 8%` day-over-day |
| C | raw close rebounds `>= 20%` from post-exit low |

Re-entry is unchanged from v2.1 (already fires on the first non-disaster day).

---

## 7. Costs

- brokerage fee `0.1425%` each side
- securities transaction tax `0.1%` on sells only

v2.1's backtest engine omitted the sell tax, slightly overstating results. v2.2's
canonical engine includes it, so all v2.2 numbers are net of fee + tax.

---

## 8. Validation (why v2.2 over v2.1)

Method: train `2015-2020` to pick params, test `2021-2026` out-of-sample (never
used for selection). Same `100萬` capital, `annual_add=0`, net of fee + sell tax.
Reproduce with `python3 optimize_h_v2_2.py`.

Best params by train Calmar — `regime_win=200, bull_exit_bonus=1, flash_mode=bull_relax` —
held up out-of-sample: higher TEST return and ~40% fewer exits than v2.1.

Honest caveat: depending on the CAGR method (XIRR vs simple) the TEST MDD reads
between roughly flat and slightly worse for v2.2. The robust, method-independent
wins are the higher out-of-sample return and the large cut in trade count (lower
cost, easier execution). v2.2 is not a free lunch on drawdown; it is a whipsaw
reduction that also lifts return.

---

## 9. Operational note

The notifier is only as good as the latest local data snapshot. If
`daily_prices_raw` or the VIX / SMH JSON is stale, computed state can lag one
session. `signal_notify.py` now needs ~400 rows of 0050 history (MA200 + backfill
headroom) — this is already handled in `load_db_prices`.
