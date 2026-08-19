# Examples

| Directory | Status | Purpose |
| --- | --- | --- |
| `foundation_taker/` | Supported foundation example | A signal-driven aggressive hedge (“taker-style”) replay through `ProductionReplayAdapter`. |
| `legacy/` | Compatibility/reference only | Historic examples that use frozen legacy backtest surfaces. They cannot produce S0 evidence. |

The foundation currently supports a passive maker on the quoted product and an
aggressive hedge on the correlated hedge product. It is not a generic
single-leg taker framework. The foundation taker example therefore demonstrates
the supported aggressive **hedge-leg** route rather than misrepresenting the
old single-leg taker as S0-compatible.

