# Foundation taker-style example

This is the supported-path successor for the *execution shape* of the historic
taker example: a causally available signal triggers an aggressive order through
`ProductionReplayAdapter`.

It is deliberately not a mechanical port of the old single-leg taker. The
current S0 foundation declares a narrower contract:

- passive maker orders belong on the quoted product; and
- aggressive “taker-style” orders belong on its correlated hedge product.

`ThresholdHedgePolicy` therefore consumes only `taker-score`, requires that
the signal itself provide its `limit_price`, and submits one aggressive hedge
when the absolute score reaches its threshold. The foundation never invents the
signal, price, action, or trigger.

## Run

From `common/`, choose an empty/new artifact directory:

```powershell
$env:PYTHONPATH = Split-Path -Parent (Get-Location)
& 'C:\Users\sgjia\miniconda3\envs\py310\python.exe' -m examples.foundation_taker.demo --artifact-root .\artifacts\foundation-taker-demo
```

The demo uses tiny synthetic inputs only to make the production route
inspectable. It writes canonical telemetry and should be operationally eligible,
but deliberately remains `economics_eligible=False`: no authenticated PnL or
valuation evidence, research export, frozen realistic episode, or holdout is
provided.

## Adapting it to real data

1. Replace `build_demo_market_data()` with
   `read_raw_snapshot_market_data()` or another strict-loader route.
2. Freeze the market/signal inputs, config, policy version, mapping, and
   execution model in provenance.
3. Pass actual `IngressEvent` signals whose payload contains a declared score
   and policy-owned limit price.
4. Add authenticated economic inputs and research export only when the
   prerequisite accounting, cycle, valuation, and semantic contracts are met.
5. Evaluate a frozen candidate on a disjoint holdout. With the available
   snapshots, claims remain bounded to the declared snapshot-interval proxy
   model, not live single-order fills or physical latency.

