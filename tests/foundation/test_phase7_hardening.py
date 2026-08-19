"""Phase-7 acceptance tests for strict loading and declared stress controls."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from common import stress as stress_module
from common.backtest import load_signals
from common.foundation_api import DualBookFoundation
from common.foundation_contracts import (
    CapacityEnvelope,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    IngressEvent,
    IngressKind,
    InstrumentSpec,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    SessionCalendar,
    TrialDeclaration,
)
from common.foundation_loader import MarketDataValidationConfig, validate_market_data
from common.ingress import CausalIngress
from common.stress import StressScenario, apply_ingress_stress, stressed_execution_models
from common.telemetry import TelemetryEmitter, load_canonical_table
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _book_row(*, contract="Q", sequence=1, milliseconds=1, bid=100.0, ask=101.0, bid_volume=3, ask_volume=4, totalvol=10, totalvalue=1000):
    return {
        "contract": contract,
        "exchange_ts": BASE_TS + timedelta(milliseconds=milliseconds - 1),
        "recv_ts": BASE_TS + timedelta(milliseconds=milliseconds),
        "source_seq": sequence,
        "bidpx0": bid,
        "bidvol0": bid_volume,
        "askpx0": ask,
        "askvol0": ask_volume,
        "totalvol": totalvol,
        "totalvalue": totalvalue,
    }


def _loader_config(**overrides) -> MarketDataValidationConfig:
    values = {"declared_contract_universe": ("Q", "H"), "book_levels": 1}
    values.update(overrides)
    return MarketDataValidationConfig(**values)


def _write_signal(path: Path, symbol: str, timestamps) -> None:
    pd.DataFrame({"pred": [0.1] * len(timestamps)}, index=pd.to_datetime(timestamps)).to_csv(path / f"pred_{symbol}.csv")


def test_signal_loader_requires_declared_universe_and_uses_declared_active_pair_order():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_signal(root, "Q", ["2025-01-02T09:00:00Z"])
        _write_signal(root, "H", ["2025-01-02T09:00:00Z"])
        signals, active = load_signals(root, declared_contract_universe=("H", "Q"))
        assert set(signals) == {"Q", "H"}
        assert tuple(active.iloc[0]) == ("H", "Q")
        try:
            load_signals(root)
        except ValueError as exc:
            assert "declared_contract_universe" in str(exc)
        else:
            raise AssertionError("signal loader must not select nth contracts without a declared universe")

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write_signal(root, "Q", ["2025-01-02T09:00:00Z"])
        try:
            load_signals(root, declared_contract_universe=("Q", "H"))
        except ValueError as exc:
            assert "missing signal coverage" in str(exc)
        else:
            raise AssertionError("an incomplete declared active pair must fail closed")


def test_strict_market_loader_has_explicit_outcomes_for_crossed_zero_and_corrected_depth():
    valid = _book_row()
    validated = validate_market_data(pd.DataFrame([valid]), _loader_config())
    events = validated.to_ingress_events(event_id_prefix="phase7")
    assert len(events) == 1 and events[0].kind is IngressKind.BOOK
    assert events[0].payload["bids"][0]["quantity"] == 3

    crossed = _book_row(bid=101.0, ask=100.0)
    try:
        validate_market_data(pd.DataFrame([crossed]), _loader_config())
    except FoundationContractError as exc:
        assert "crossed_book" in str(exc)
    else:
        raise AssertionError("crossed book must reach an explicit reject outcome")

    zero = _book_row(contract="H", sequence=2, milliseconds=2, bid_volume=0)
    dropped = validate_market_data(
        pd.DataFrame([valid, zero]),
        _loader_config(zero_depth_disposition="drop"),
    )
    assert dropped.accepted_rows == 1 and dropped.dropped_rows == 1
    assert dropped.issues[0].code == "zero_top_depth" and dropped.issues[0].disposition == "drop"

    reset = _book_row(sequence=2, milliseconds=2, totalvol=9, totalvalue=900)
    try:
        validate_market_data(pd.DataFrame([valid, reset]), _loader_config())
    except FoundationContractError as exc:
        assert "cumulative_volume_reset" in str(exc)
    else:
        raise AssertionError("cumulative-volume correction must have an explicit reject outcome")


def test_stress_dimensions_are_independent_and_participation_models_are_versioned():
    try:
        StressScenario("phase7-receive-delay", "1.0.0", market_data_delay_ms=5)
    except FoundationContractError as exc:
        assert "receive-time market-data and signal delays" in str(exc)
    else:
        raise AssertionError("exchange-batch replay must reject receive-time market delay")
    scenario = StressScenario(
        "phase7-stress",
        "1.0.0",
        action_submission_delay_ms=2,
        action_arrival_delay_ms=3,
        participation_multiplier=0.5,
        fee_multiplier=0.25,
        basis_shift=2.0,
        volatility_multiplier=0.5,
        opening_session_disposition="skip",
    )
    events = (
        IngressEvent(
            "phase7-book",
            "Q",
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS,
            1,
            {
                "bids": [{"price": 90.0, "quantity": 1}],
                "asks": [{"price": 110.0, "quantity": 1}],
                "passive_trades": [{"trade_id": "phase7-trade", "taker_side": "sell", "price": 100.5, "quantity": 1}],
            },
        ),
        IngressEvent("phase7-signal", "Q", IngressKind.SIGNAL, BASE_TS, BASE_TS, 2, {"signal_id": "s"}),
    )
    stressed_spec = InstrumentSpec("Q", 1.0, 1.0, SessionCalendar("phase7-utc", "UTC"), "fees", "roll")
    stressed_book, stressed_signal = apply_ingress_stress(
        events,
        scenario,
        instrument_specs={"Q": stressed_spec},
    )
    assert stressed_book.exchange_ts == events[0].exchange_ts
    assert stressed_book.recv_ts == events[0].recv_ts
    assert stressed_book.payload["bids"][0]["price"] == 95.0
    assert stressed_book.payload["asks"][0]["price"] == 105.0
    assert stressed_book.payload["passive_trades"][0]["price"] == 100.0
    assert stressed_signal.recv_ts == events[1].recv_ts
    assert scenario.submission_at(BASE_TS) == BASE_TS + timedelta(milliseconds=2)
    assert scenario.arrival_at(BASE_TS) == BASE_TS + timedelta(milliseconds=5)
    assert scenario.adjusted_fee(4.0) == 1.0
    assert scenario.adjusted_decision_mid(100.0) == 102.0
    assert scenario.adjusted_price(110.0, reference_price=100.0) == 105.0
    assert not scenario.admits_opening_session()

    model = ExecutionModelConfig("depth", "1.0.0", 0.8, metadata={"unchanged": "yes"})
    stressed = stressed_execution_models((model,), ExecutionModelRef("depth", "1.0.0"), scenario)
    assert stressed.models[0].participation_rate == 0.4
    assert stressed.models[0].model_id == model.model_id
    assert stressed.models[0].sparse_book_disposition == model.sparse_book_disposition
    assert stressed.default_execution_model.version != model.version
    assert stressed.reference_for(ExecutionModelRef("depth", "1.0.0")) == stressed.default_execution_model


def test_post_transform_validator_rejects_off_tick_book_and_passive_trade():
    specification = InstrumentSpec("Q", 1.0, 1.0, SessionCalendar("phase7-validator", "UTC"), "fees", "roll")
    off_tick_book = IngressEvent(
        "phase7-off-tick-book",
        "Q",
        IngressKind.BOOK,
        BASE_TS,
        BASE_TS,
        1,
        {"bids": [{"price": 100.25, "quantity": 1}], "asks": [{"price": 101.0, "quantity": 1}]},
    )
    try:
        stress_module._validate_stressed_book_event(off_tick_book, specification)
    except FoundationContractError as exc:
        assert str(exc) == "stressed_bid_price_off_tick"
    else:
        raise AssertionError("post-transform validator must reject an off-tick stressed book")

    off_tick_trade = IngressEvent(
        "phase7-off-tick-trade",
        "Q",
        IngressKind.BOOK,
        BASE_TS,
        BASE_TS,
        2,
        {
            "bids": [{"price": 100.0, "quantity": 1}],
            "asks": [{"price": 101.0, "quantity": 1}],
            "passive_trades": [{"taker_side": "sell", "price": 100.25, "quantity": 1}],
        },
    )
    try:
        stress_module._validate_stressed_book_event(off_tick_trade, specification)
    except FoundationContractError as exc:
        assert str(exc) == "stressed_passive_trade_off_tick"
    else:
        raise AssertionError("post-transform validator must reject an off-tick passive trade")


def test_facade_stress_is_emitted_and_hashed_with_its_trial_configuration():
    fixture = make_dual_book_fixture()
    scenario = StressScenario(
        "phase7-facade",
        "1.0.0",
        action_submission_delay_ms=1,
        action_arrival_delay_ms=1,
        fee_multiplier=0.5,
        basis_shift=2.0,
    )
    model = ExecutionModelConfig("phase7-depth", "1.0.0", 1.0)
    stressed_models = stressed_execution_models((model,), ExecutionModelRef(model.model_id, model.version), scenario)
    events = (
        IngressEvent(
            "phase7-q-book",
            fixture.quoted_spec.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=2),
            1,
            {"bids": [{"price": 78000.0, "quantity": 2}], "asks": [{"price": 78005.0, "quantity": 2}]},
        ),
        IngressEvent(
            "phase7-h-book",
            fixture.hedge_spec.product,
            IngressKind.BOOK,
            BASE_TS,
            BASE_TS + timedelta(milliseconds=3),
            2,
            {"bids": [{"price": 77980.0, "quantity": 2}], "asks": [{"price": 77985.0, "quantity": 2}]},
        ),
    )
    with TemporaryDirectory() as temporary:
        emitter = TelemetryEmitter(Path(temporary), "phase7-stress-run", fixture.hedge_pair)
        api = DualBookFoundation(
            run_id="phase7-stress-run",
            hedge_mapping=HedgeMappingSpec(fixture.hedge_pair, 1.0, 1.0),
            instrument_specs=(fixture.quoted_spec, fixture.hedge_spec),
            execution_models=(model,),
            default_execution_model=ExecutionModelRef(model.model_id, model.version),
            capacity_envelopes=(CapacityEnvelope("phase7-cap", fixture.hedge_pair, fixture.quoted_spec.product, 2),),
            telemetry=emitter,
            stress_scenario=scenario,
        )
        ingress = CausalIngress("phase7-stress-run", events)
        tuple(ingress.replay())
        context = ingress.decision_context("phase7-decision", fixture.hedge_pair)
        for event in events:
            api.record_book_event(event, ingress.book_ref_for_event(event.event_id))
        for ref in (context.quoted_book, context.hedge_book):
            api.record_book_snapshot(ref, ingress.book_snapshot(ref))
            api.ingest_depth_from_snapshot(ref)
        hedge = OrderIntent(
            "phase7-hedge",
            context.run_id,
            context.decision_id,
            fixture.hedge_pair,
            fixture.hedge_spec.product,
            OrderRole.HEDGE,
            OrderSide.SELL,
            1,
            77980.0,
        )
        api.submit(MakerHedgeIntentBatch(hedge_intent=hedge), context, occurred_at=context.dec_ts)
        api.arrive(hedge.intent_id, occurred_at=context.dec_ts + timedelta(milliseconds=1))
        result = api.execute_hedge(
            hedge.intent_id,
            executed_at=context.dec_ts + timedelta(milliseconds=3),
            decision_mid=77980.0,
            fee=2.0,
        )
        api.record_inventory("phase7-inventory", occurred_at=context.dec_ts + timedelta(milliseconds=4))
        api.record_unattributed_outcome("phase7-outcome", "stress conformance has no PnL attribution")
        api.capture_provenance(
            TrialDeclaration(
                "phase7-trial",
                "2025-01-01:2025-01-31",
                "2025-02-01:2025-02-14",
                "2025-02-15:2025-02-28",
                "frozen-configuration-hash",
                "phase7-policy",
                fixture.hedge_pair,
                (stressed_models.default_execution_model,),
                ("strict-loader",),
            ),
            {
                "market_data": "phase7-market",
                "signal_data": "phase7-signal",
                "configuration": {"candidate": "frozen-v1"},
                "code": "phase7-code",
                "schema": "telemetry-schema-v0.4",
                "fee_profile": "phase7-fees",
                "instrument_roll_mapping": "phase7-roll",
                "execution_models": "phase7-depth-1.0.0",
            },
        )
        final = api.finalize()
        trigger_rows = tuple(load_canonical_table(emitter.run_dir, "trigger_evaluations"))
        provenance = emitter.provenance

    assert result.decision_mid == 77982.0
    assert api.ledger_state().total_fees == 1.0
    assert final.eligible
    assert any(row["attributes"]["record_type"] == "stress_scenario" for row in trigger_rows)
    assert provenance is not None and "stress_scenario" in provenance.artifact_hashes


def test_immutable_provenance_distinguishes_a_frozen_candidate_from_later_development_configuration():
    fixture = make_dual_book_fixture()
    trial = TrialDeclaration(
        "phase7-freeze-trial",
        "2025-01-01:2025-01-31",
        "2025-02-01:2025-02-14",
        "2025-02-15:2025-02-28",
        "frozen-candidate-v1",
        "phase7-policy",
        fixture.hedge_pair,
        (ExecutionModelRef("phase7-depth", "1.0.0"),),
        ("strict-loader",),
    )
    artifacts = {
        "market_data": "phase7-market",
        "signal_data": "phase7-signal",
        "configuration": {"candidate": "frozen-v1"},
        "code": "phase7-code",
        "schema": "telemetry-schema-v0.4",
        "fee_profile": "phase7-fees",
        "instrument_roll_mapping": "phase7-roll",
        "execution_models": "phase7-depth-1.0.0",
    }
    with TemporaryDirectory() as temporary:
        frozen = TelemetryEmitter(Path(temporary), "phase7-frozen", fixture.hedge_pair)
        frozen_provenance = frozen.capture_provenance(trial, artifacts)
        later = TelemetryEmitter(Path(temporary), "phase7-later-development", fixture.hedge_pair)
        later_provenance = later.capture_provenance(trial, {**artifacts, "configuration": {"candidate": "later-v2"}})

    assert frozen_provenance.provenance_hash != later_provenance.provenance_hash
    assert frozen_provenance.artifact_hashes["configuration"] != later_provenance.artifact_hashes["configuration"]
