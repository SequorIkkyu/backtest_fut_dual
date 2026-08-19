# Legacy examples

This directory contains historical strategy examples retained for code reading,
diagnostics, and comparison.

They use the frozen `public_tools`/legacy backtest surfaces and are not
adapters to the maker-hedger foundation. Their results must not be presented as
S0 economic, stress, holdout, or promotion evidence.

- `arb/` is the original spread-arbitrage baseline.
- `taker/` is the historical single-leg taker experiment. It still expects
  configuration and utility modules that are outside this repository package.

Use `examples/foundation_taker/` for a runnable signal-driven example on the
supported production replay path.

