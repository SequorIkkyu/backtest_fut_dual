# `common/` engine tests

> **Maker-hedger acceptance command (Phases 0-7):** run both this legacy suite and
> the dedicated foundation contract/characterization suite with Python 3.10.13:
>
> ```powershell
> $env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
> & C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance
> ```
>
> The adjacent `strategies.pairs.test_pairs_v2` behavioural suite is archived
> reference code, excluded from supported commands, and is not an S0 adapter. See
> [`docs/maker-hedger-foundation-boundary_26-08-08.md`](../docs/maker-hedger-foundation-boundary_26-08-08.md).

A small set of automated checks for the **core of the backtest engine**. Each
test sets up a tiny, known scenario, runs one piece of the engine, and asserts
the result is exactly what it should be. If every check passes, the engine still
behaves the way it did when these were written.

---

## New to test suites? Start here

**What a test is.** A test is just a short program that exercises a piece of the
code with known inputs and then *asserts* (demands) a specific output. For
example: "rest a 1-lot bid, trade one lot at that price, and the engine must
report exactly one fill of one lot." If the engine returns that, the test
*passes* silently; if it returns anything else, the test *fails* loudly and
points at the line that broke.

**Why this matters here specifically.** This engine is the thing that turns
market data into PnL. A subtle mistake in it doesn't crash — it just produces
*wrong numbers* that look completely plausible. You could refactor the matcher,
re-run a strategy, and get a Sharpe that's quietly off because fills now happen a
tick too eagerly, or a fee is charged twice. There's no error message for "this
backtest is silently lying to you." These tests are that missing error message:
they pin down the exact behaviors every backtest depends on, so a future change
that breaks one of them gets caught in seconds instead of contaminating months of
results.

Concretely, the suite protects you from regressions like:

- a fill that should wait behind the queue happening immediately (overstated fills),
- the round-trip fee getting charged on both the open *and* the close (double fee drag),
- a position flip leaving a stale average cost (wrong realized PnL),
- the night session and the next day landing in different order-count buckets
  (wrong order-limit accounting).

**The payoff.** Once these exist, you can change engine internals with
confidence: make your edit, run the suite, and a green result means you didn't
silently alter how fills, PnL, fees, sessions, or cycles work. A red result tells
you *exactly* what changed before it reaches a real backtest.

---

## What's covered

| File | What it pins down |
|------|-------------------|
| `test_market.py` | The matching engine (`common/market.py`): how an order's queue is built (your size sits *behind* the volume already resting at that price), how the queue decays as the market trades, when a passive order fills (full and partial), price-priority "trade-through" fills, aggressive/crossing fills at the touch, FAK (fill-and-kill) marketable / capped / rejected, and order-message counting (post / cancel / missing timestamp / disabled tracker). |
| `test_update_pos.py` | The PnL engine (`common/strategy.py::update_pos`): weighted-average entry cost for longs and shorts, realized PnL when a position closes, the **round-trip fee charged once at the close** (both the per-lot `FEE_LOT` and the rate-based `FEE`), long↔short position flips resetting cost correctly, `opened_qty` tracking, and several fills in one update. |
| `test_sessions.py` | The session calendar (`common/sessions.py`): which hours count as day vs night, and the `+6h` "trading day" rule that makes a 21:00 night action and the next day's 09:00–15:00 action share one trading-day bucket. |
| `test_cycles.py` | The cycle normalizer (`common/cycles.py`): turning each strategy's own trade-record shape (taker vs pairs) into the one schema the reports rely on, and the per-bucket PnL summary. |

Supporting files: `helpers.py` (builders that fabricate market snapshots and a
`Market`/`Strategy` to drive), `run.py` (the runner), this `README.md`.

---

## How to run it

The legacy suite runs under the supported Python 3.10.13 (`py310`) foundation
runtime. For the complete Phase-0/Phase-7 acceptance check, use the command at the top
of this document. To run only this legacy suite, set the backtest repository
root on `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run
```

You do **not** need to install anything — `run.py` is a self-contained runner.
(The test files are also written as standard `pytest` functions, so if you ever
`pip install pytest` you can run `pytest common/tests` instead; same files, same
results.)

### Reading the output

```
test_market  (45 tests)
  ok    test_aggressive_buy_fills_at_best_ask
  ok    test_partial_fill_when_queue_below_qty
  ...
============================================================
72/72 passed, 0 failed
```

- **`ok`** — that check passed.
- **`FAIL <name>: AssertionError: ...`** — that check found the engine doing
  something other than what the test demands. The runner prints the full
  traceback for each failure at the bottom, with the file and line of the failing
  `assert` so you can see exactly which behavior changed.
- The last line is the scoreboard. **`72/72 passed`** is the legacy-suite green light. The aggregate acceptance command also reports the foundation suite. Any
  non-zero "failed" count means stop and look — either your change altered engine
  behavior (fix the code) or it intentionally changed a rule (update the test).

The process also exits with code `0` on all-pass and `1` on any failure, so it
can gate a commit hook or CI step later.

### When to run it

- **After editing anything in `common/`** — especially `market.py`, `strategy.py`,
  `sessions.py`, or `cycles.py`. This is the main use: prove your change didn't
  move a number it shouldn't have.
- **Before trusting a fresh batch of backtest results** following engine work.
- **When a backtest result looks surprising** — a green suite tells you the engine
  primitives are sound, so the surprise is in the strategy or the data, not the
  matcher/PnL.

It takes a second or two; there's no reason not to run it.

---

## How it works (so the tests stay trustworthy)

The tests use **no market-data files**. Each one fabricates a tiny order-book
snapshot in code and drives the real engine directly, so a test is fast,
deterministic, and readable end-to-end — you can see the entire scenario in a few
lines. The builders live in `helpers.py`:

- `make_record(bid0=..., bidvol=..., askvol=..., traded_px=..., traded_v1=...)` —
  one market-data snapshot: a 5-level book around `bid0` (best ask is `bid0 + 1
  tick`), plus an optional "a trade happened at this price/size" component used to
  decay queues. `bid0` must be a clean tick multiple.
- `new_market(record)` — a fresh `Market` with that snapshot loaded as the current
  state, ready for `place_order` / `fak` / `cancel_*` / `match`.
- `feed(market, record)` — push a *new* snapshot and run `Market.step()` (this is
  what decays resting orders' queue positions as the market trades).
- `snap(px)` — round a price to the tick grid (handy when asserting on book keys).

---

## Adding your own test

Adding a check is the normal way to lock in a new engine behavior (or to reproduce
a bug before fixing it). Drop a function named `test_*` into the matching file —
the runner discovers it automatically. Use plain `assert`.

A matching-engine example — *"a 1-lot bid fills once a lot trades at its price"*:

```python
from common.tests.helpers import make_record, new_market, feed

def test_bid_fills_after_one_lot_trades():
    m = new_market(make_record(bid0=100.0, bidvol=0))   # empty queue ahead of us
    bid0 = m.curr["X"]["bidpx0"]
    m.place_order("X", bid0, 1)                          # rest a 1-lot bid (queue = 1)
    feed(m, make_record(bid0=100.0, bidvol=0,            # one lot trades at our price
                        traded_px=bid0, traded_v1=1.0))  #   -> our queue decays to 0
    fills = m.match("X")
    assert len(fills) == 1 and fills[0]["qty"] == 1      # we got filled, 1 lot
```

A PnL-engine example — *"a round trip is charged the fee once, at the close"*:

```python
from common.market import Market
from common.strategy import Strategy

def test_round_trip_fee_charged_once():
    s = Strategy("T", Market(mult=10000, tick=0.005), 10000, 0.005, None, 0.1, fee_lot=3.0)
    s.reset("X")
    s.update_pos([{"px": 100.0, "qty": 1}])    # open  -> no fee
    assert s.total_fees == 0
    s.update_pos([{"px": 101.0, "qty": -1}])   # close -> fee charged here, once
    assert s.total_fees == 3.0
```

Tips:
- Keep each test to one behavior with an obvious name — when it fails, the name
  should tell you what broke.
- If you're fixing a bug, first add a test that fails for the current code, then
  fix the code until it passes. That guarantees the bug can't silently come back.

---

## Conventions these tests pin

These are the engine rules the suite guards. If you intend to change one, change
the test in the same commit so the new rule is the one that's enforced.

- **Fees are round-trip, charged once on the close.** `FEE` / `FEE_LOT` are the
  *full* round-trip cost; opens add no fee (see the repo-root `CLAUDE.md`).
- **FAK fills `round(level_vol * FAK_AVAIL)`**, with `FAK_AVAIL = 0.5` — a taker
  order gets at most half the displayed top-of-book size.
- **Trading day = night session + the following day session**, via the `+6h`
  shift (so the day boundary falls at 18:00).

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'common'`** — `PYTHONPATH` isn't set to
  both source roots. Re-run the `$env:PYTHONPATH = ...` line above in the same
  shell before invoking python.
- **A test fails after an engine change you made on purpose** — that's the suite
  doing its job. Decide whether the *code* is wrong (revert/fix it) or the *rule*
  genuinely changed (update the assert, and ideally note why in the commit).
- **You want the standard test runner** — `pip install pytest` into `py310`, then
  `pytest common/tests`. The bundled `run.py` exists so the suite works even
  without it.
