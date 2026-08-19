"""Run a small, source-contained production-replay taker-style example.

The synthetic rows make this executable without a vendor feed. They demonstrate
the route and artifact shape only, so the returned result is intentionally
operational/non-economic. A realistic study must use the strict raw-snapshot
adapter, frozen inputs, PnL evidence, research export, and holdout evaluation.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pandas as pd

from common.foundation_contracts import (
    CapacityEnvelope,
    ExecutionModelConfig,
    ExecutionModelRef,
    HedgeMappingSpec,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    SessionCalendar,
    TELEMETRY_SCHEMA_VERSION,
    TrialDeclaration,
)
from common.foundation_loader import MarketDataValidationConfig, ValidatedMarketData, validate_market_data
from common.production_replay import OperationalReplayResult, ProductionReplayAdapter, ProductionReplayConfig
from examples.foundation_taker.taker_policy import ThresholdHedgePolicy


_DEMO_TIME = datetime(2025, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
_DEMO_RUN_ID = "foundation-taker-demo"


def build_demo_config(artifact_root: str | Path) -> tuple[ProductionReplayConfig, HedgePairRef, ExecutionModelRef]:
    """Build a complete non-economic production replay configuration."""
    calendar = SessionCalendar("foundation-taker-demo-utc", "UTC", eod_time=time(9, 0, 10))
    quoted = InstrumentSpec("DEMO-Q", 1.0, 1.0, calendar, "demo-fees", "demo-roll")
    hedge = InstrumentSpec("DEMO-H", 1.0, 1.0, calendar, "demo-fees", "demo-roll")
    pair = HedgePairRef("foundation-taker-demo-pair", quoted.product, hedge.product, "demo-map", "1.0.0")
    model = ExecutionModelConfig("foundation-taker-demo-depth", "1.0.0", participation_rate=1.0)
    model_ref = ExecutionModelRef(model.model_id, model.version)
    trial = TrialDeclaration(
        "foundation-taker-demo-trial",
        "demo-development",
        "demo-calibration",
        "demo-holdout",
        "not-a-promotion-candidate",
        "threshold-hedge-demo-v1",
        pair,
        (model_ref,),
        ("strict-loader",),
    )
    config = ProductionReplayConfig(
        run_id=_DEMO_RUN_ID,
        hedge_mapping=HedgeMappingSpec(pair, quoted_risk_weight=1.0, hedge_risk_weight=1.0),
        instrument_specs=(quoted, hedge),
        execution_models=(model,),
        default_execution_model=model_ref,
        capacity_envelopes=(CapacityEnvelope("foundation-taker-demo-quoted-cap", pair, quoted.product, 1),),
        artifact_root=artifact_root,
        session_date=date(2025, 1, 2),
        trial=trial,
        provenance_artifacts={
            "market_data": "synthetic-demo-market-data",
            "signal_data": "synthetic-demo-taker-score",
            "configuration": "foundation-taker-demo-config-v1",
            "code": "foundation-taker-demo-code-v1",
            "schema": f"telemetry-schema-v{TELEMETRY_SCHEMA_VERSION}",
            "fee_profile": "demo-fees",
            "instrument_roll_mapping": "demo-roll",
            "execution_models": "foundation-taker-demo-depth-v1",
        },
        registered_signal_ids=frozenset({"taker-score"}),
        max_execution_book_age_ms_by_product={quoted.product: 1_000.0, hedge.product: 1_000.0},
    )
    return config, pair, model_ref


def build_demo_market_data() -> ValidatedMarketData:
    """Return strict-loader-validated dual-book data with retained hedge depth."""
    rows = (
        {
            "contract": "DEMO-Q",
            "exchange_ts": _DEMO_TIME,
            "recv_ts": _DEMO_TIME + timedelta(milliseconds=1),
            "source_seq": 1,
            "bidpx0": 100.0,
            "bidvol0": 2,
            "askpx0": 101.0,
            "askvol0": 2,
            "totalvol": 1,
            "totalvalue": 100.0,
            "passive_trades": (),
        },
        {
            "contract": "DEMO-H",
            "exchange_ts": _DEMO_TIME + timedelta(milliseconds=4),
            "recv_ts": _DEMO_TIME + timedelta(milliseconds=6),
            "source_seq": 5,
            "bidpx0": 99.0,
            "bidvol0": 3,
            "askpx0": 100.0,
            "askvol0": 3,
            "totalvol": 1,
            "totalvalue": 99.0,
            "passive_trades": (),
        },
        {
            "contract": "DEMO-H",
            "exchange_ts": _DEMO_TIME,
            "recv_ts": _DEMO_TIME + timedelta(milliseconds=2),
            "source_seq": 2,
            "bidpx0": 99.0,
            "bidvol0": 3,
            "askpx0": 100.0,
            "askvol0": 3,
            "totalvol": 1,
            "totalvalue": 99.0,
            "passive_trades": (),
        },
        {
            "contract": "DEMO-Q",
            "exchange_ts": _DEMO_TIME + timedelta(milliseconds=4),
            "recv_ts": _DEMO_TIME + timedelta(milliseconds=4),
            "source_seq": 4,
            "bidpx0": 100.0,
            "bidvol0": 2,
            "askpx0": 101.0,
            "askvol0": 2,
            "totalvol": 2,
            "totalvalue": 200.0,
            "passive_trades": (),
        },
    )
    return validate_market_data(pd.DataFrame(rows), MarketDataValidationConfig(("DEMO-Q", "DEMO-H")))


def build_demo_signal_events() -> tuple[IngressEvent, ...]:
    """Create one causally available policy-owned taker score and limit."""
    return (
        IngressEvent(
            "foundation-taker-demo:signal:1",
            "DEMO-H",
            IngressKind.SIGNAL,
            _DEMO_TIME + timedelta(milliseconds=3),
            _DEMO_TIME + timedelta(milliseconds=3),
            3,
            {"signal_id": "taker-score", "score": 1.0, "limit_price": 98.0},
        ),
    )


def run_demo(artifact_root: str | Path) -> OperationalReplayResult:
    """Execute the example through the sole supported production route."""
    config, pair, model_ref = build_demo_config(artifact_root)
    policy = ThresholdHedgePolicy(pair, model_ref)
    return ProductionReplayAdapter(config).run(
        build_demo_market_data(),
        policy,
        signal_events=build_demo_signal_events(),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the foundation taker-style hedge demo.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts") / _DEMO_RUN_ID,
        help="Empty or new directory for canonical demo artifacts.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_demo(args.artifact_root)
    print(f"artifacts: {args.artifact_root / _DEMO_RUN_ID}")
    print(f"decisions: {len(result.decision_ids)}")
    print(f"aggressive hedge executions: {len(result.execution_ids)}")
    print(f"canonical telemetry eligible: {result.telemetry.eligible}")
    print(f"economics eligible: {result.economics_eligible} (intentional demo; no authenticated PnL or research export)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
