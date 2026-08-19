"""Supported strict-loader to calendar-EOD operational replay tests."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date, time, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from common import production_replay as production_replay_module
from common.foundation_contracts import (
    ApprovedEvidenceAuthority,
    CapacityEnvelope,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    HedgePairRef,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    PnlAccountingView,
    PnlViewEvidence,
    SessionCalendar,
    SessionWindow,
    TrialDeclaration,
    ValuationMarkEvidence,
)
from common.foundation_api import PolicyProposal, PolicyTrigger
from common.foundation_loader import MarketDataValidationConfig, validate_market_data
from common.production_replay import (
    DeploymentEvidenceAuthorityRegistry,
    EconomicReplayInputs,
    ProductionReplayAdapter,
    ProductionReplayConfig,
)
from common.research_telemetry import RESEARCH_S0_TABLES, S0_SEMANTIC_COMPLIANCE_VERSION
from common.stress import StressScenario
from common.telemetry import load_canonical_table
from common.tests.foundation.fixtures import BASE_TS


_ACCOUNTING_AUTHORITY = ApprovedEvidenceAuthority("fixture-accounting", "v1", b"fixture-accounting-key")
_CYCLE_AUTHORITY = ApprovedEvidenceAuthority("fixture-cycle", "v1", b"fixture-cycle-auth-key")
_VALUATION_AUTHORITY = ApprovedEvidenceAuthority("fixture-valuation", "v1", b"fixture-valuation-key")
_EVIDENCE_AUTHORITIES = (_ACCOUNTING_AUTHORITY, _CYCLE_AUTHORITY, _VALUATION_AUTHORITY)
_DEPLOYMENT_AUTHORITY_REGISTRY = DeploymentEvidenceAuthorityRegistry(_EVIDENCE_AUTHORITIES)


def _signed_economic_artifact(authority, **fields) -> bytes:
    unsigned = {
        "schema_version": "s0-economic-evidence-v1",
        "authority_id": authority.authority_id,
        "key_id": authority.key_id,
        **fields,
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return json.dumps(
        {**unsigned, "signature": authority.sign(canonical)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _MakerOncePolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    def propose(self, context):
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch())
        self._used = True
        maker = OrderIntent(
            "replay-maker",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.quoted_product,
            OrderRole.MAKER,
            OrderSide.BUY,
            1,
            100.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(maker_intent=maker, maker_capacity_envelope_id="replay-quoted-cap"))

    @staticmethod
    def select_signal_ids(available_signals):
        return ()


class _HedgeOncePolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    def propose(self, context):
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch())
        self._used = True
        hedge = OrderIntent(
            "replay-hedge",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            1,
            97.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(hedge_intent=hedge))

    @staticmethod
    def select_signal_ids(available_signals):
        return ()


class _SignalAwareNoActionPolicy:
    def __init__(self) -> None:
        self.available_signal_ids: list[tuple[str, ...]] = []
        self.context_signal_ids: list[tuple[str, ...]] = []
        self.context_signal_scores: list[tuple[float, ...]] = []

    def select_signal_ids(self, available_signals):
        selected = tuple(signal.signal_id for signal in available_signals if signal.signal_id == "inventory-signal")
        self.available_signal_ids.append(selected)
        return selected

    def propose(self, context):
        consumed = tuple(signal.signal_id for signal in context.consumed_signals)
        self.context_signal_ids.append(consumed)
        self.context_signal_scores.append(
            tuple(float(context.signal_value(signal).payload["score"]) for signal in context.consumed_signals)
        )
        return PolicyProposal(
            MakerHedgeIntentBatch(),
            {"action": "hold", "signal_set_hash": "fixture-signals"},
            (PolicyTrigger(f"{context.decision_id}:trigger", {"trigger_class": "none", "fired": False}),),
        )


class _ResearchNoActionPolicy:
    @staticmethod
    def select_signal_ids(available_signals):
        return ()

    @staticmethod
    def propose(context):
        return PolicyProposal(
            MakerHedgeIntentBatch(),
            {
                "side": "buy",
                "action": "no_trade",
                "quote_price": None,
                "size": None,
                "quote_age_ms": None,
                "queue_ahead": None,
                "reservation_price": 100.5,
                "skew": 0.0,
                "cap_state": "within_cap",
                "capacity_reserved": 0.0,
                "block_reason": "no_edge",
                "cancel_reason": None,
                "trigger_priority": None,
                "hysteresis_state": "stable",
            },
        )


class _ResearchHedgeOncePolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    @staticmethod
    def select_signal_ids(available_signals):
        return ()

    def propose(self, context):
        common = {
            "side": "sell",
            "action": "hedge" if not self._used else "no_trade",
            "quote_price": None,
            "size": None,
            "quote_age_ms": None,
            "queue_ahead": None,
            "reservation_price": 99.5,
            "skew": 0.0,
            "cap_state": "within_cap",
            "capacity_reserved": 0.0,
            "block_reason": None if not self._used else "no_edge",
            "cancel_reason": None,
            "trigger_priority": None,
            "hysteresis_state": "stable",
        }
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch(), common)
        self._used = True
        common.update(
            {
                "hedge_trigger_class": "value_edge",
                "hedge_target_before": 0,
                "hedge_target_after": -1,
                "hedge_retry_count": 0,
                "hedge_deadline_ts": None,
            }
        )
        hedge = OrderIntent(
            "research-hedge",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            1,
            97.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(hedge_intent=hedge), common)


class _ResearchSignalNoActionPolicy(_ResearchNoActionPolicy):
    @staticmethod
    def select_signal_ids(available_signals):
        return tuple(signal.signal_id for signal in available_signals if signal.signal_id == "research-signal")


class _HedgeNoFillPolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    @staticmethod
    def select_signal_ids(available_signals):
        return ()

    def propose(self, context):
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch())
        self._used = True
        hedge = OrderIntent(
            "freshness-hedge",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            1,
            101.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(hedge_intent=hedge))


class _HedgeQuantityPolicy:
    def __init__(self, pair, model, quantity: int = 2) -> None:
        self._pair = pair
        self._model = model
        self._quantity = quantity
        self._used = False

    @staticmethod
    def select_signal_ids(available_signals):
        return ()

    def propose(self, context):
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch())
        self._used = True
        hedge = OrderIntent(
            "participation-hedge",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            self._quantity,
            97.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(hedge_intent=hedge))


class _ResearchMakerOncePolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    @staticmethod
    def select_signal_ids(available_signals):
        return ()

    def propose(self, context):
        attributes = {
            "side": "buy",
            "action": "quote" if not self._used else "no_trade",
            "quote_price": 100.0 if not self._used else None,
            "size": 1 if not self._used else None,
            "quote_age_ms": 0.0 if not self._used else None,
            "queue_ahead": 0.0 if not self._used else None,
            "reservation_price": 100.5,
            "skew": 0.0,
            "cap_state": "within_cap",
            "capacity_reserved": 1.0 if not self._used else 0.0,
            "block_reason": None if not self._used else "no_edge",
            "cancel_reason": None,
            "trigger_priority": None,
            "hysteresis_state": "stable",
        }
        if self._used:
            return PolicyProposal(MakerHedgeIntentBatch(), attributes)
        self._used = True
        maker = OrderIntent(
            "research-maker",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.quoted_product,
            OrderRole.MAKER,
            OrderSide.BUY,
            1,
            100.0,
            self._model,
        )
        return PolicyProposal(MakerHedgeIntentBatch(maker_intent=maker, maker_capacity_envelope_id="replay-quoted-cap"), attributes)


class _SignalDrivenMakerHedgePolicy:
    def __init__(self, pair, model) -> None:
        self._pair = pair
        self._model = model
        self._used = False

    @staticmethod
    def select_signal_ids(available_signals):
        return tuple(signal.signal_id for signal in available_signals if signal.signal_id == "research-signal")

    def propose(self, context):
        score = 0.0
        selected = bool(context.consumed_signals)
        if selected:
            bound_signal = context.signal_value(context.consumed_signals[0])
            try:
                score = float(bound_signal.payload["score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FoundationContractError("research-signal policy requires a numeric bound score") from exc
        active = selected and score > 0.0 and not self._used
        attributes = {
            "side": "buy",
            "action": "quote" if active else "no_trade",
            "quote_price": 100.0 if active else None,
            "size": 1 if active else None,
            "quote_age_ms": 0.0 if active else None,
            "queue_ahead": 0.0 if active else None,
            "reservation_price": 100.5,
            "skew": 0.0,
            "cap_state": "within_cap",
            "capacity_reserved": 1.0 if active else 0.0,
            "block_reason": None if active else "signal_unavailable_or_consumed",
            "cancel_reason": None,
            "trigger_priority": "value_edge" if active else None,
            "hysteresis_state": "armed" if active else "idle",
        }
        if not active:
            return PolicyProposal(MakerHedgeIntentBatch(), attributes)
        self._used = True
        trigger_id = f"{context.decision_id}:value-edge"
        attributes.update(
            {
                "hedge_trigger_id": trigger_id,
                "hedge_trigger_class": "value_edge",
                "hedge_target_before": 0,
                "hedge_target_after": -1,
                "hedge_retry_count": 0,
                "hedge_deadline_ts": None,
            }
        )
        maker = OrderIntent(
            "signal-maker",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.quoted_product,
            OrderRole.MAKER,
            OrderSide.BUY,
            1,
            100.0,
            self._model,
        )
        hedge = OrderIntent(
            "signal-hedge",
            context.run_id,
            context.decision_id,
            self._pair,
            self._pair.hedge_product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            1,
            97.0,
            self._model,
        )
        trigger = PolicyTrigger(
            trigger_id,
            {
                "trigger_class": "value_edge",
                "inputs": {"signal_score": score},
                "fired": True,
                "target": -1,
                "reason": "signal_edge",
                "hysteresis_state": "armed",
                "cooldown_ms": 0,
            },
        )
        return PolicyProposal(MakerHedgeIntentBatch(maker, hedge, "replay-quoted-cap"), attributes, (trigger,))


def _config(root: Path):
    calendar = SessionCalendar("replay-utc", "UTC", eod_time=time(9, 0, 10))
    quoted = InstrumentSpec("Q", 1.0, 1.0, calendar, "fees", "roll")
    hedge = InstrumentSpec("H", 1.0, 1.0, calendar, "fees", "roll")
    pair = HedgePairRef("replay-pair", "Q", "H", "calendar", "1.0.0")
    model = ExecutionModelConfig("replay-depth", "1.0.0", 1.0)
    model_ref = ExecutionModelRef(model.model_id, model.version)
    trial = TrialDeclaration(
        "replay-trial",
        "development",
        "calibration",
        "holdout",
        "freeze",
        "replay-policy",
        pair,
        (model_ref,),
        ("strict-loader",),
    )
    config = ProductionReplayConfig(
        "production-replay",
        HedgeMappingSpec(pair, 1.0, 1.0),
        (quoted, hedge),
        (model,),
        model_ref,
        (CapacityEnvelope("replay-quoted-cap", pair, "Q", 1),),
        root,
        date(2025, 1, 2),
        trial,
        {
            "market_data": "validated-fixture",
            "signal_data": "no-signals",
            "configuration": "replay-config",
            "code": "replay-code",
            "schema": "schema-v0.4",
            "fee_profile": "fees",
            "instrument_roll_mapping": "roll",
            "execution_models": "replay-depth-v1",
        },
        max_execution_book_age_ms_by_product={"Q": 1_000.0, "H": 1_000.0},
    )
    return config, pair, model_ref


def _market_data():
    rows = [
        {
            "contract": "Q", "exchange_ts": BASE_TS, "recv_ts": BASE_TS + timedelta(milliseconds=1), "source_seq": 1,
            "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2, "totalvol": 1, "totalvalue": 100,
            "passive_trades": [],
        },
        {
            "contract": "H", "exchange_ts": BASE_TS, "recv_ts": BASE_TS + timedelta(milliseconds=2), "source_seq": 2,
            "bidpx0": 99.0, "bidvol0": 3, "askpx0": 100.0, "askvol0": 3, "totalvol": 1, "totalvalue": 99,
            "passive_trades": [],
        },
        {
            "contract": "Q", "exchange_ts": BASE_TS + timedelta(milliseconds=3), "recv_ts": BASE_TS + timedelta(milliseconds=4), "source_seq": 3,
            "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2, "totalvol": 2, "totalvalue": 200,
            "passive_trades": [{"trade_id": "replay-trade", "taker_side": "sell", "price": 100.0, "quantity": 3}],
        },
        {
            "contract": "H", "exchange_ts": BASE_TS + timedelta(milliseconds=3), "recv_ts": BASE_TS + timedelta(milliseconds=4), "source_seq": 4,
            "bidpx0": 99.0, "bidvol0": 3, "askpx0": 100.0, "askvol0": 3, "totalvol": 2, "totalvalue": 198,
            "passive_trades": [],
        },
    ]
    return validate_market_data(
        pd.DataFrame(rows),
        MarketDataValidationConfig(("Q", "H")),
    )


def _provenanced_economic_inputs(
    *,
    run_id: str,
    accounting_total: float,
    cycle_total: float,
    authorities: tuple[ApprovedEvidenceAuthority, ApprovedEvidenceAuthority, ApprovedEvidenceAuthority] = _EVIDENCE_AUTHORITIES,
) -> EconomicReplayInputs:
    eod_at = BASE_TS + timedelta(seconds=10)
    accounting_authority, cycle_authority, valuation_authority = authorities
    return EconomicReplayInputs(
        {"Q": 100.5, "H": 99.5},
        PnlAccountingView("accounting", accounting_total),
        PnlAccountingView("cycle", cycle_total),
        accounting_evidence=PnlViewEvidence(
            "accounting-evidence",
            "accounting",
            accounting_total,
            "general-ledger",
            "1.0.0",
            "general-ledger-close",
            eod_at,
            _signed_economic_artifact(
                accounting_authority,
                artifact_type="pnl_view",
                run_id=run_id,
                session_date="2025-01-02",
                evidence_id="accounting-evidence",
                view_id="accounting",
                total_pnl=accounting_total,
                methodology="general-ledger",
                methodology_version="1.0.0",
                source_artifact_id="general-ledger-close",
                calculated_at=eod_at.isoformat(),
            ),
        ),
        cycle_evidence=PnlViewEvidence(
            "cycle-evidence",
            "cycle",
            cycle_total,
            "cycle-reconciliation",
            "1.0.0",
            "cycle-reconciliation-close",
            eod_at,
            _signed_economic_artifact(
                cycle_authority,
                artifact_type="pnl_view",
                run_id=run_id,
                session_date="2025-01-02",
                evidence_id="cycle-evidence",
                view_id="cycle",
                total_pnl=cycle_total,
                methodology="cycle-reconciliation",
                methodology_version="1.0.0",
                source_artifact_id="cycle-reconciliation-close",
                calculated_at=eod_at.isoformat(),
            ),
        ),
        mark_evidence_by_product={
            "Q": ValuationMarkEvidence(
                "q-mark",
                "Q",
                100.5,
                "settlement",
                "1.0.0",
                "q-settlement",
                BASE_TS,
                _signed_economic_artifact(
                    valuation_authority,
                    artifact_type="valuation_mark",
                    run_id=run_id,
                    session_date="2025-01-02",
                    evidence_id="q-mark",
                    product="Q",
                    mark=100.5,
                    methodology="settlement",
                    methodology_version="1.0.0",
                    source_artifact_id="q-settlement",
                    observed_at=BASE_TS.isoformat(),
                ),
            ),
            "H": ValuationMarkEvidence(
                "h-mark",
                "H",
                99.5,
                "settlement",
                "1.0.0",
                "h-settlement",
                BASE_TS,
                _signed_economic_artifact(
                    valuation_authority,
                    artifact_type="valuation_mark",
                    run_id=run_id,
                    session_date="2025-01-02",
                    evidence_id="h-mark",
                    product="H",
                    mark=99.5,
                    methodology="settlement",
                    methodology_version="1.0.0",
                    source_artifact_id="h-settlement",
                    observed_at=BASE_TS.isoformat(),
                ),
            ),
        },
    )


def test_production_replay_uses_strict_ingress_verified_fill_and_calendar_eod():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        result = ProductionReplayAdapter(config).run(_market_data(), _MakerOncePolicy(pair, model_ref))
        fill_rows = tuple(load_canonical_table(Path(temporary) / config.run_id, "fills"))
        controls = json.loads((Path(temporary) / config.run_id / "meta" / "run_controls.json").read_text())

    assert result.telemetry.eligible
    assert not result.economics_eligible
    assert result.eod_completion.disposition.value == "flat"
    assert result.passive_fill_ids == ("production-replay:replay-maker:replay-trade:1",)
    assert result.execution_ids == ()
    assert len(result.decision_ids) == 3
    evidence = next(row for row in fill_rows if row["record_type"] == "passive_match_evidence")
    maker_ledger = next(
        row for row in fill_rows if row["record_type"] == "ledger_effect" and row.get("order_role") == "maker"
    )
    assert maker_ledger["matched_passive_fill_id"] == evidence["fill_id"]
    assert controls == {"require_verified_passive_fills": True}


def test_production_replay_has_no_legacy_runtime_imports():
    tree = ast.parse(Path(production_replay_module.__file__).read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)

    assert not imports & {"common.backtest", "common.market", "common.strategy", "public_tools"}


def test_production_replay_preserves_state_across_break_without_deciding_or_accepting_break_books():
    calendar = SessionCalendar(
        "replay-breaks",
        "UTC",
        windows=(
            SessionWindow("opening", time(9, 0, 0), time(9, 0, 1)),
            SessionWindow("afternoon", time(9, 0, 3), time(9, 0, 9)),
        ),
        eod_time=time(9, 0, 10),
    )
    signal = IngressEvent(
        "break-signal",
        "Q",
        IngressKind.SIGNAL,
        BASE_TS + timedelta(seconds=2),
        BASE_TS + timedelta(seconds=2),
        5,
        {"signal_id": "inventory-signal", "score": 0.25},
    )
    rows = _market_data().frame.to_dict("records")
    rows.append(
        {
            "contract": "Q", "exchange_ts": BASE_TS + timedelta(seconds=4),
            "recv_ts": BASE_TS + timedelta(seconds=4, milliseconds=1), "source_seq": 4,
            "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2,
            "totalvol": 3, "totalvalue": 300, "passive_trades": [],
        }
    )
    rows.append(
        {
            "contract": "H", "exchange_ts": BASE_TS + timedelta(seconds=4),
            "recv_ts": BASE_TS + timedelta(seconds=4, milliseconds=2), "source_seq": 5,
            "bidpx0": 99.0, "bidvol0": 3, "askpx0": 100.0, "askvol0": 3,
            "totalvol": 3, "totalvalue": 297, "passive_trades": [],
        }
    )
    market_data = validate_market_data(pd.DataFrame(rows), MarketDataValidationConfig(("Q", "H")))

    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        session_config = replace(
            config,
            run_id="production-replay-session-break",
            instrument_specs=tuple(replace(spec, calendar=calendar) for spec in config.instrument_specs),
        )
        policy = _SignalAwareNoActionPolicy()
        result = ProductionReplayAdapter(session_config).run(market_data, policy, signal_events=(signal,))

        invalid_rows = list(rows)
        invalid_rows.append(
            {
                "contract": "Q", "exchange_ts": BASE_TS + timedelta(seconds=2),
                "recv_ts": BASE_TS + timedelta(seconds=2, milliseconds=1), "source_seq": 6,
                "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2,
                "totalvol": 3, "totalvalue": 300, "passive_trades": [],
            }
        )
        invalid_rows.append(
            {
                "contract": "H", "exchange_ts": BASE_TS + timedelta(seconds=2),
                "recv_ts": BASE_TS + timedelta(seconds=2, milliseconds=2), "source_seq": 7,
                "bidpx0": 99.0, "bidvol0": 3, "askpx0": 100.0, "askvol0": 3,
                "totalvol": 2, "totalvalue": 198, "passive_trades": [],
            }
        )
        invalid_rows.sort(key=lambda row: (pd.Timestamp(row["recv_ts"]), int(row["source_seq"])))
        invalid_data = validate_market_data(pd.DataFrame(invalid_rows), MarketDataValidationConfig(("Q", "H")))
        try:
            ProductionReplayAdapter(replace(session_config, run_id="production-replay-break-book")).run(
                invalid_data, _SignalAwareNoActionPolicy()
            )
        except FoundationContractError as exc:
            assert "outside the declared product session" in str(exc)
        else:
            raise AssertionError("a book event during a declared session break must fail closed")

        try:
            ProductionReplayAdapter(
                replace(
                    session_config,
                    run_id="production-replay-break-arrival",
                    stress_scenario=StressScenario("break-arrival", "1.0.0", action_arrival_delay_ms=2_000.0),
                )
            ).run(_market_data(), _HedgeOncePolicy(pair, model_ref))
        except FoundationContractError as exc:
            assert "scheduled policy action occurs during a declared session break" in str(exc)
        else:
            raise AssertionError("a delayed policy action during a session break must fail closed")

    assert policy.context_signal_ids == [(), (), ("inventory-signal",)]
    assert len(result.decision_ids) == 4
    assert result.eod_completion.completed_at == BASE_TS + timedelta(seconds=10)


def test_production_replay_applies_declared_fee_stress_to_matcher_issued_maker_evidence():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        stressed = replace(
            config,
            run_id="production-replay-maker-fee-stress",
            passive_fee_rebate_per_contract=0.4,
            stress_scenario=StressScenario("maker-fee", "1.0.0", fee_multiplier=0.5),
        )
        ProductionReplayAdapter(stressed).run(_market_data(), _MakerOncePolicy(pair, model_ref))
        fill_rows = tuple(load_canonical_table(Path(temporary) / stressed.run_id, "fills"))

    evidence = next(row for row in fill_rows if row["record_type"] == "passive_match_evidence")
    assert evidence["fee_rebate"] == 0.2


def test_production_replay_applies_volatility_and_opening_session_stress_to_real_decisions():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        volatility = replace(
            config,
            run_id="production-replay-volatility",
            stress_scenario=StressScenario("volatility", "1.0.0", volatility_multiplier=0.5),
        )
        opening_skip = replace(
            config,
            run_id="production-replay-opening-skip",
            stress_scenario=StressScenario("opening-skip", "1.0.0", opening_session_disposition="skip"),
        )
        volatile_result = ProductionReplayAdapter(volatility).run(_market_data(), _MakerOncePolicy(pair, model_ref))
        skipped_result = ProductionReplayAdapter(opening_skip).run(_market_data(), _MakerOncePolicy(pair, model_ref))

    assert volatile_result.passive_fill_ids
    assert skipped_result.decision_ids == (
        "production-replay-opening-skip:decision:000001",
        "production-replay-opening-skip:eod:2025-01-02",
    )
    assert skipped_result.passive_fill_ids == ()


def test_production_replay_rejects_receive_time_delay_stress_and_applies_participation_stress():
    signal = IngressEvent(
        "signal-delay-event",
        "Q",
        IngressKind.SIGNAL,
        BASE_TS + timedelta(milliseconds=2),
        BASE_TS + timedelta(milliseconds=3),
        3,
        {
            "signal_id": "research-signal",
            "model_version": "model-1",
            "feature_version": "features-1",
            "source": "fixture",
            "score": 0.25,
            "regime": "normal",
            "calibration_bucket": "base",
            "feature_coverage": 1.0,
        },
    )
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        base_participation = replace(config, run_id="production-replay-participation-base")
        stressed_participation = replace(
            config,
            run_id="production-replay-participation-stress",
            stress_scenario=StressScenario("participation", "1.0.0", participation_multiplier=0.5),
        )
        try:
            delayed_signal = replace(
                config,
                run_id="production-replay-signal-delay",
                stress_scenario=StressScenario("signal-delay", "1.0.0", signal_delay_ms=5.0),
            )
            ProductionReplayAdapter(delayed_signal).run(
                _market_data(), _SignalDrivenMakerHedgePolicy(pair, model_ref), signal_events=(signal,)
            )
        except FoundationContractError as exc:
            assert "receive-time market-data and signal delays" in str(exc)
        else:
            raise AssertionError("exchange-batch replay must reject receive-time signal delay")
        ProductionReplayAdapter(base_participation).run(_market_data(), _HedgeQuantityPolicy(pair, model_ref))
        ProductionReplayAdapter(stressed_participation).run(_market_data(), _HedgeQuantityPolicy(pair, model_ref))
        base_rows = tuple(load_canonical_table(Path(temporary) / base_participation.run_id, "hedge_executions"))
        stressed_rows = tuple(load_canonical_table(Path(temporary) / stressed_participation.run_id, "hedge_executions"))

    base_execution = next(row for row in base_rows if row["order_id"] == "participation-hedge")
    stressed_execution = next(row for row in stressed_rows if row["order_id"] == "participation-hedge")
    assert base_execution["filled_qty"] == 2
    assert stressed_execution["filled_qty"] == 1


def test_stressed_replay_uses_book_available_before_delayed_hedge_arrival():
    rows = [
        {
            "contract": "Q", "exchange_ts": BASE_TS, "recv_ts": BASE_TS + timedelta(milliseconds=1), "source_seq": 1,
            "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2, "totalvol": 1, "totalvalue": 100,
        },
        {
            "contract": "H", "exchange_ts": BASE_TS, "recv_ts": BASE_TS + timedelta(milliseconds=2), "source_seq": 2,
            "bidpx0": 99.0, "bidvol0": 2, "askpx0": 100.0, "askvol0": 2, "totalvol": 1, "totalvalue": 99,
        },
        {
            "contract": "Q", "exchange_ts": BASE_TS + timedelta(milliseconds=3), "recv_ts": BASE_TS + timedelta(milliseconds=4), "source_seq": 3,
            "bidpx0": 100.0, "bidvol0": 2, "askpx0": 101.0, "askvol0": 2, "totalvol": 2, "totalvalue": 200,
        },
        {
            "contract": "H", "exchange_ts": BASE_TS + timedelta(milliseconds=3), "recv_ts": BASE_TS + timedelta(milliseconds=4), "source_seq": 4,
            "bidpx0": 97.0, "bidvol0": 2, "askpx0": 98.0, "askvol0": 2, "totalvol": 2, "totalvalue": 196,
        },
    ]
    data = validate_market_data(pd.DataFrame(rows), MarketDataValidationConfig(("Q", "H")))
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        stressed = replace(
            config,
            run_id="production-replay-stressed",
            stress_scenario=StressScenario("arrival-delay", "1.0.0", action_arrival_delay_ms=5),
        )
        result = ProductionReplayAdapter(stressed).run(data, _HedgeOncePolicy(pair, model_ref))
        executions = tuple(load_canonical_table(Path(temporary) / stressed.run_id, "hedge_executions"))

    assert result.telemetry.eligible
    assert len(result.execution_ids) == 1
    assert executions[0]["vwap"] == 97.0
    assert executions[0]["book_snapshot_id"].endswith(":book:2")


def test_production_replay_applies_submission_and_arrival_delay_exactly_once():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        stressed = replace(
            config,
            run_id="production-replay-action-timing",
            stress_scenario=StressScenario(
                "action-timing", "1.0.0", action_submission_delay_ms=2.0, action_arrival_delay_ms=3.0
            ),
        )
        ProductionReplayAdapter(stressed).run(_market_data(), _HedgeOncePolicy(pair, model_ref))
        orders = tuple(load_canonical_table(Path(temporary) / stressed.run_id, "orders"))
        executions = tuple(load_canonical_table(Path(temporary) / stressed.run_id, "hedge_executions"))

    lifecycle = {
        row["lifecycle_state"]: row
        for row in orders
        if row["order_id"] == "replay-hedge" and row["record_type"] == "lifecycle"
    }
    assert lifecycle["submitted"]["occurred_at"] == (BASE_TS + timedelta(milliseconds=2)).isoformat()
    assert lifecycle["arrived"]["occurred_at"] == (BASE_TS + timedelta(milliseconds=5)).isoformat()
    assert executions[0]["executed_at"] == (BASE_TS + timedelta(milliseconds=5)).isoformat()


def test_production_replay_rejects_market_delay_and_applies_basis_shift_on_its_owned_route():
    economic_inputs = EconomicReplayInputs(
        {"Q": 100.5, "H": 99.5}, PnlAccountingView("accounting", 0.0), PnlAccountingView("cycle", 0.0)
    )
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        base = replace(config, run_id="production-replay-basis-base", economic_inputs=economic_inputs)
        shifted_basis = replace(
            config,
            run_id="production-replay-basis-shift",
            economic_inputs=economic_inputs,
            stress_scenario=StressScenario("basis-shift", "1.0.0", basis_shift=2.0),
        )
        try:
            delayed_market = replace(
                config,
                run_id="production-replay-market-delay",
                stress_scenario=StressScenario("market-delay", "1.0.0", market_data_delay_ms=5.0),
            )
            ProductionReplayAdapter(delayed_market).run(_market_data(), _HedgeOncePolicy(pair, model_ref))
        except FoundationContractError as exc:
            assert "receive-time market-data and signal delays" in str(exc)
        else:
            raise AssertionError("exchange-batch replay must reject receive-time market delay")
        base_result = ProductionReplayAdapter(base).run(_market_data(), _HedgeOncePolicy(pair, model_ref))
        shifted_result = ProductionReplayAdapter(shifted_basis).run(_market_data(), _HedgeOncePolicy(pair, model_ref))
    assert base_result.pnl_attribution is not None and base_result.pnl_attribution.hedge_execution_shortfall == 1.0
    assert shifted_result.pnl_attribution is not None and shifted_result.pnl_attribution.hedge_execution_shortfall == 3.0


def test_production_replay_binds_policy_declared_causal_signals_and_trigger_metadata():
    with TemporaryDirectory() as temporary:
        config, _, _ = _config(Path(temporary))
        signal = IngressEvent(
            "inventory-signal-event",
            "Q",
            IngressKind.SIGNAL,
            BASE_TS + timedelta(milliseconds=2),
            BASE_TS + timedelta(milliseconds=3),
            3,
            {"signal_id": "inventory-signal", "score": 0.25},
        )
        policy = _SignalAwareNoActionPolicy()
        result = ProductionReplayAdapter(config).run(_market_data(), policy, signal_events=(signal,))
        decisions = tuple(load_canonical_table(Path(temporary) / config.run_id, "decisions"))
        triggers = tuple(load_canonical_table(Path(temporary) / config.run_id, "trigger_evaluations"))

    assert result.telemetry.eligible
    assert any(ids == ("inventory-signal",) for ids in policy.context_signal_ids)
    assert any(scores == (0.25,) for scores in policy.context_signal_scores)
    assert any(row["consumed_signal_snapshot_ids"] for row in decisions)
    assert all(row["attributes"]["trigger_class"] == "none" for row in triggers if "attributes" in row)


def test_production_replay_runs_declared_independent_pnl_attribution_but_remains_fail_closed_without_research_export():
    with TemporaryDirectory() as temporary:
        config, _, _ = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-pnl",
            economic_inputs=_provenanced_economic_inputs(
                run_id="production-replay-pnl", accounting_total=0.0, cycle_total=0.0
            ),
        )
        result = ProductionReplayAdapter(economic).run(_market_data(), _SignalAwareNoActionPolicy())
        outcomes = tuple(load_canonical_table(Path(temporary) / economic.run_id, "outcome_pnl"))

    assert result.pnl_attribution is not None and result.pnl_attribution.economics_eligible
    assert outcomes[0]["attribution_status"] == "reconciled"
    assert not result.economics_eligible


def test_production_replay_seals_research_export_before_claiming_economics_eligibility():
    with TemporaryDirectory() as temporary:
        config, _, _ = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-research",
            economic_inputs=_provenanced_economic_inputs(
                run_id="production-replay-research", accounting_total=0.0, cycle_total=0.0
            ),
            research_export=True,
        )
        result = ProductionReplayAdapter(economic, authority_registry=_DEPLOYMENT_AUTHORITY_REGISTRY).run(
            _market_data(), _ResearchNoActionPolicy()
        )
        research_root = Path(temporary) / "research" / economic.run_id
        tables = {path.stem for path in (research_root / "tables").glob("*.jsonl")}
        result_payload = json.loads((research_root / "meta" / "research_result.json").read_text())
        manifest_bytes = (research_root / "meta" / "research_manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        table_hashes = {
            table: f"sha256:{hashlib.sha256((research_root / 'tables' / f'{table}.jsonl').read_bytes()).hexdigest()}"
            for table in RESEARCH_S0_TABLES
        }
        table_hashes["label_outcomes"] = None

    assert result.telemetry.eligible
    assert result.pnl_attribution is not None and result.pnl_attribution.economics_eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert result.semantic_compliance_eligible
    assert result.economics_eligible
    assert {
        "s0_semantic_compliance",
        "pnl_accounting_view_evidence",
        "pnl_accounting_view_source",
        "pnl_cycle_view_evidence",
        "pnl_cycle_view_source",
        "valuation_mark_evidence",
        "valuation_mark_source_0",
        "valuation_mark_source_1",
        "approved_evidence_authorities",
        "research_manifest",
    } <= set(result.telemetry.provenance.artifact_hashes)
    assert tables == set(RESEARCH_S0_TABLES)
    assert result.research_telemetry.manifest_hash == f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    assert result.telemetry.provenance.artifact_hashes["research_manifest"] == result.research_telemetry.manifest_hash
    assert result_payload == {
        "run_id": economic.run_id,
        "eligible": True,
        "errors": [],
        "semantic_compliance_version": S0_SEMANTIC_COMPLIANCE_VERSION,
        "research_manifest_hash": result.research_telemetry.manifest_hash,
    }
    assert manifest["research_result"] == {
        "run_id": economic.run_id,
        "eligible": True,
        "errors": [],
        "semantic_compliance_version": S0_SEMANTIC_COMPLIANCE_VERSION,
    }
    assert set(manifest["tables"]) == set(table_hashes)
    assert manifest["tables"] == table_hashes


def test_production_replay_rejects_caller_supplied_self_signed_economic_evidence():
    with TemporaryDirectory() as temporary:
        config, _, _ = _config(Path(temporary))
        run_id = "production-replay-self-signed-evidence"
        caller_authorities = (
            ApprovedEvidenceAuthority("caller-accounting", "v1", b"caller-accounting-key"),
            ApprovedEvidenceAuthority("caller-cycle", "v1", b"caller-cycle-auth-key"),
            ApprovedEvidenceAuthority("caller-valuation", "v1", b"caller-valuation-key"),
        )
        caller_signed = _provenanced_economic_inputs(
            run_id=run_id,
            accounting_total=0.0,
            cycle_total=0.0,
            authorities=caller_authorities,
        )
        economic = replace(config, run_id=run_id, economic_inputs=caller_signed, research_export=True)
        result = ProductionReplayAdapter(economic, authority_registry=_DEPLOYMENT_AUTHORITY_REGISTRY).run(
            _market_data(), _ResearchNoActionPolicy()
        )

    assert result.pnl_attribution is not None and result.pnl_attribution.economics_eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert result.execution_freshness_eligible
    assert not result.semantic_compliance_eligible
    assert not result.economics_eligible


def test_research_export_reconstructs_active_hedge_orders_executions_and_inventory_before_pnl_gate():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-research-hedge",
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.5, "H": 99.5}, PnlAccountingView("accounting", 0.0), PnlAccountingView("cycle", 0.0)
            ),
            research_export=True,
        )
        result = ProductionReplayAdapter(economic).run(_market_data(), _ResearchHedgeOncePolicy(pair, model_ref))
        research_root = Path(temporary) / "research" / economic.run_id
        orders = tuple(json.loads(line) for line in (research_root / "tables" / "orders.jsonl").read_text().splitlines())
        executions = tuple(json.loads(line) for line in (research_root / "tables" / "hedge_executions.jsonl").read_text().splitlines())
        inventory = tuple(json.loads(line) for line in (research_root / "tables" / "inventory_series.jsonl").read_text().splitlines())

    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert {row["order_id"] for row in orders} >= {"research-hedge", f"{economic.run_id}:eod:{economic.session_date.isoformat()}:hedge"}
    assert len(executions) == 2
    assert any(row["event_source"] == "hedge" and row["h"] == -1 for row in inventory)
    assert not result.semantic_compliance_eligible
    assert not result.economics_eligible


def test_active_dual_leg_replay_claims_economics_only_after_pnl_research_and_freshness_all_reconcile():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-eligible-hedge",
            economic_inputs=_provenanced_economic_inputs(
                run_id="production-replay-eligible-hedge", accounting_total=-1.0, cycle_total=-1.0
            ),
            research_export=True,
            max_execution_book_age_ms_by_product={"Q": 20_000.0, "H": 20_000.0},
        )
        result = ProductionReplayAdapter(economic, authority_registry=_DEPLOYMENT_AUTHORITY_REGISTRY).run(
            _market_data(), _ResearchHedgeOncePolicy(pair, model_ref)
        )

    assert result.pnl_attribution is not None and result.pnl_attribution.economics_eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert result.execution_freshness_eligible
    assert result.economics_eligible


def test_research_export_links_only_policy_declared_causal_signal_snapshot_metadata():
    signal = IngressEvent(
        "research-signal-event",
        "Q",
        IngressKind.SIGNAL,
        BASE_TS + timedelta(milliseconds=2),
        BASE_TS + timedelta(milliseconds=3),
        3,
        {
            "signal_id": "research-signal",
            "model_version": "model-1",
            "feature_version": "features-1",
            "source": "fixture",
            "score": 0.25,
            "regime": "normal",
            "calibration_bucket": "base",
            "feature_coverage": 1.0,
        },
    )
    with TemporaryDirectory() as temporary:
        config, _, _ = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-research-signal",
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.5, "H": 99.5}, PnlAccountingView("accounting", 0.0), PnlAccountingView("cycle", 0.0)
            ),
            research_export=True,
            registered_signal_ids=frozenset({"research-signal"}),
        )
        result = ProductionReplayAdapter(economic).run(_market_data(), _ResearchSignalNoActionPolicy(), signal_events=(signal,))
        research_root = Path(temporary) / "research" / economic.run_id
        signal_rows = tuple(
            json.loads(line) for line in (research_root / "tables" / "signal_snapshots.jsonl").read_text().splitlines()
        )

    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert signal_rows and all(row["signal_id"] == "research-signal" for row in signal_rows)
    assert all(row["age_ms"] >= 0 and row["model_version"] == "model-1" for row in signal_rows)


def test_declared_per_product_book_age_threshold_gates_delayed_execution_freshness():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        fresh = replace(
            config,
            run_id="production-replay-freshness-base",
            max_execution_book_age_ms_by_product={"Q": 1.0, "H": 1.0},
        )
        delayed = replace(
            fresh,
            run_id="production-replay-freshness-delayed",
            stress_scenario=StressScenario("freshness-delay", "1.0.0", action_arrival_delay_ms=5.0),
        )
        fresh_result = ProductionReplayAdapter(fresh).run(_market_data(), _HedgeNoFillPolicy(pair, model_ref))
        delayed_result = ProductionReplayAdapter(delayed).run(_market_data(), _HedgeNoFillPolicy(pair, model_ref))

    assert fresh_result.execution_freshness_eligible
    assert not delayed_result.execution_freshness_eligible
    assert not delayed_result.economics_eligible


def test_research_export_reconstructs_verified_maker_and_eod_taker_fill_chain():
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-research-maker",
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.5, "H": 99.5}, PnlAccountingView("accounting", 0.0), PnlAccountingView("cycle", 0.0)
            ),
            research_export=True,
        )
        result = ProductionReplayAdapter(economic).run(_market_data(), _ResearchMakerOncePolicy(pair, model_ref))
        research_root = Path(temporary) / "research" / economic.run_id
        fills = tuple(json.loads(line) for line in (research_root / "tables" / "fills.jsonl").read_text().splitlines())

    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert {(row["liquidity_role"], row["product"]) for row in fills} == {("maker", "Q"), ("taker", "Q")}
    assert not result.economics_eligible


def test_signal_driven_dual_leg_replay_reconciles_authoritative_policy_trigger_and_basis_evidence():
    signal = IngressEvent(
        "signal-driven-event",
        "Q",
        IngressKind.SIGNAL,
        BASE_TS + timedelta(milliseconds=2),
        BASE_TS + timedelta(milliseconds=3),
        3,
        {
            "signal_id": "research-signal",
            "model_version": "model-1",
            "feature_version": "features-1",
            "source": "fixture",
            "score": 0.25,
            "regime": "normal",
            "calibration_bucket": "base",
            "feature_coverage": 1.0,
        },
    )
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        provisional = replace(
            config,
            run_id="production-replay-signal-driven",
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.5, "H": 99.5}, PnlAccountingView("accounting", 0.0), PnlAccountingView("cycle", 0.0)
            ),
            research_export=True,
            registered_signal_ids=frozenset({"research-signal"}),
            max_execution_book_age_ms_by_product={"Q": 20_000.0, "H": 20_000.0},
        )
        result = ProductionReplayAdapter(provisional).run(
            _market_data(), _SignalDrivenMakerHedgePolicy(pair, model_ref), signal_events=(signal,)
        )
        research_root = Path(temporary) / "research" / provisional.run_id
        decisions = tuple(json.loads(line) for line in (research_root / "tables" / "decisions.jsonl").read_text().splitlines())
        triggers = tuple(json.loads(line) for line in (research_root / "tables" / "trigger_evaluations.jsonl").read_text().splitlines())
        hedges = tuple(json.loads(line) for line in (research_root / "tables" / "hedge_executions.jsonl").read_text().splitlines())

    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert result.pnl_attribution is not None and result.pnl_attribution.waterfall_total == -1.0
    active = next(row for row in decisions if row["action"] == "quote")
    assert active["signal_set_hash"].startswith("sha256:")
    assert any(row["trigger_id"].endswith(":value-edge") and row["fired"] for row in triggers)
    assert any(row["trigger_class"] == "value_edge" and row["basis_at_fill"] == 1.0 for row in hedges)
    assert not result.economics_eligible


def test_signal_driven_dual_leg_replay_claims_economics_only_when_its_independent_views_reconcile():
    signal = IngressEvent(
        "signal-driven-eligible-event",
        "Q",
        IngressKind.SIGNAL,
        BASE_TS + timedelta(milliseconds=2),
        BASE_TS + timedelta(milliseconds=3),
        3,
        {
            "signal_id": "research-signal",
            "model_version": "model-1",
            "feature_version": "features-1",
            "source": "fixture",
            "score": 0.25,
            "regime": "normal",
            "calibration_bucket": "base",
            "feature_coverage": 1.0,
        },
    )
    with TemporaryDirectory() as temporary:
        config, pair, model_ref = _config(Path(temporary))
        economic = replace(
            config,
            run_id="production-replay-signal-driven-eligible",
            economic_inputs=_provenanced_economic_inputs(
                run_id="production-replay-signal-driven-eligible", accounting_total=-1.0, cycle_total=-1.0
            ),
            research_export=True,
            registered_signal_ids=frozenset({"research-signal"}),
            max_execution_book_age_ms_by_product={"Q": 20_000.0, "H": 20_000.0},
        )
        result = ProductionReplayAdapter(economic, authority_registry=_DEPLOYMENT_AUTHORITY_REGISTRY).run(
            _market_data(), _SignalDrivenMakerHedgePolicy(pair, model_ref), signal_events=(signal,)
        )

    assert result.pnl_attribution is not None and result.pnl_attribution.economics_eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert result.execution_freshness_eligible
    assert result.economics_eligible
