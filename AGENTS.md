# Working in `common/`

## Purpose and boundary

`common/` contains the maker-hedger foundation. The sole supported S0 evidence
path is:

```text
strict loader -> CausalIngress -> ProductionReplayAdapter -> DualBookFoundation
              -> calendar EOD -> PnL attribution -> canonical + research telemetry
```

`production_replay.py` is the operational entry point. Keep it isolated from
`backtest.py`, `market.py`, `strategy.py`, `PairMarket`, and `public_tools/`.
Those modules are frozen compatibility/example paths and must never generate or
be described as S0 economics, stress, holdout, or promotion evidence.

The governing status is in
[`docs/maker-hedger-s0-operational-remediation-plan_26-08-08.md`](docs/maker-hedger-s0-operational-remediation-plan_26-08-08.md).
Deterministic fixtures validate infrastructure; they do not substitute for a
frozen realistic policy/data episode or holdout evidence.

## Before editing

1. Inspect `git status --short`; preserve unrelated work, including untracked
   local-tool folders.
2. Read the relevant public contract before changing a boundary:
   `foundation_contracts.py`, `foundation_api.py`, `telemetry.py`, and/or
   `research_telemetry.py`.
3. Prefer the supported foundation route. Do not repair an S0 issue by copying
   behavior into a legacy class.

## Implementation rules

- Keep causal time explicit. Exchange time identifies a market event; receive
  time/availability controls decisions and execution.
- A policy may consume only declared, causally available signal IDs. Policy
  fields (action, reservation, trigger data) must be supplied by the policy;
  the foundation must not invent them.
- Production maker fills require matcher-issued `PassiveFillEvidence`. Preserve
  source-event-qualified trade identity, price-time priority, and shared trade
  quantity conservation.
- Aggressive and EOD execution must use retained, causally available,
  depth-consuming books. Do not synthesize fills or restore consumed depth.
- `economics_eligible` must stay fail-closed: canonical telemetry, PnL,
  research telemetry, and execution-freshness gates must all pass.
- Research telemetry is a separate, semantic contract. Changes to an exported
  field require type/nullability/enum/join/timing validation and a producer-map
  update.
- Stress controls must remain independent and content-hashed. In production,
  scheduler-owned submission/arrival timing is applied exactly once.
- Enforce product sessions in production replay: retain state through a break,
  make no decision during it, and fail closed on a scheduled action arriving in
  it.

## Tests and verification

Run the full acceptance command after any foundation, replay, telemetry,
matching, stress, or session change:

```powershell
$env:PYTHONPATH = Split-Path -Parent (Get-Location)
& 'C:\Users\sgjia\miniconda3\envs\py310\python.exe' -m common.tests.run_acceptance
git diff --check
```

The suite reports legacy and foundation counts separately. Update documented
counts only after a successful full run. Targeted probes are useful while
developing, but do not replace this command.

## Documentation and handoff

- Update the operational plan when a remediation item genuinely closes or a
  remaining gate changes.
- Do not claim S0 promotion from synthetic fixtures. The remaining promotion
  evidence must use frozen realistic inputs and a holdout evaluation.
- Document any new configuration, artifact, or invariant alongside its test.
- Do not commit, reset, delete, or modify unrelated user changes unless asked.
