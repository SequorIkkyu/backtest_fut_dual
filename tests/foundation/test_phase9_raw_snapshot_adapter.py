"""Phase-9 acceptance coverage for the supported raw-snapshot evidence bridge."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time, timedelta
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from common.foundation_api import PolicyProposal
from common.foundation_contracts import (
    CapacityEnvelope,
    ExecutionModelConfig,
    ExecutionModelRef,
    FoundationContractError,
    HedgeMappingSpec,
    HedgePairRef,
    InstrumentSpec,
    MakerHedgeIntentBatch,
    OrderIntent,
    OrderRole,
    OrderSide,
    PnlAccountingView,
    SessionCalendar,
    SnapshotInterval,
    SnapshotIntervalPriceBucket,
    TrialDeclaration,
)
from common.foundation_loader import (
    LoaderValidationIssue,
    RawSnapshotAdapterConfig,
    RawSnapshotFile,
    RawSnapshotFrame,
    adapt_raw_snapshot_frames,
    adapted_replay_events_hash,
    read_raw_snapshot_market_data,
)
from common.production_replay import EconomicReplayInputs, ProductionReplayAdapter, ProductionReplayConfig
from common.passive_matching import PassiveMatchingService
from common.telemetry import load_canonical_table
from common.tests.foundation.fixtures import BASE_TS, make_dual_book_fixture


def _clock(milliseconds: int) -> str:
    return (BASE_TS + timedelta(milliseconds=milliseconds)).replace(tzinfo=None).isoformat()


def _raw_row(
    *,
    timestamp_ms: int,
    exchange_ms: int,
    totalvol: float,
    totalvalue: float,
    bid: float,
    ask: float,
    lastpx: float,
    openinterest: float,
) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": _clock(timestamp_ms),
        "exchtime": _clock(exchange_ms),
        "totalvol": totalvol,
        "totalvalue": totalvalue,
        "lastpx": lastpx,
        "openinterest": openinterest,
    }
    for level in range(5):
        row.update(
            {
                f"bidpx{level}": bid - level,
                f"bidvol{level}": 2,
                f"askpx{level}": ask + level,
                f"askvol{level}": 2,
            }
        )
    return row


def _raw_config(**overrides) -> RawSnapshotAdapterConfig:
    values = {
        "declared_contract_universe": ("Q", "H"),
        "proxy_interval_contracts": ("Q",),
        "source_timezone": "UTC",
        "tick_by_contract": {"Q": 1.0, "H": 1.0},
        "multiplier_by_contract": {"Q": 1.0, "H": 1.0},
    }
    values.update(overrides)
    return RawSnapshotAdapterConfig(**values)


def _raw_sources(
    *, invalid_interval: bool = False, cumulative_reset: bool = False, off_depth_interval: bool = False, hedge_second_row: bool = False
) -> tuple[RawSnapshotFrame, RawSnapshotFrame]:
    q_second_volume = 11.5 if invalid_interval else 13.0
    q_second_value = 1150.0 if invalid_interval else 1270.0 if off_depth_interval else 1298.0
    quoted_rows = [
        _raw_row(
            timestamp_ms=0,
            exchange_ms=0,
            totalvol=10.0,
            totalvalue=1000.0,
            bid=100.0,
            ask=101.0,
            lastpx=100.0,
            openinterest=50.0,
        ),
        _raw_row(
            timestamp_ms=4,
            exchange_ms=3,
            totalvol=q_second_volume,
            totalvalue=q_second_value,
            bid=100.0,
            ask=101.0,
            lastpx=101.0,
            openinterest=51.0,
        ),
    ]
    if cumulative_reset:
        quoted_rows = [
            quoted_rows[0],
            _raw_row(
                timestamp_ms=4,
                exchange_ms=3,
                totalvol=5.0,
                totalvalue=500.0,
                bid=100.0,
                ask=101.0,
                lastpx=101.0,
                openinterest=51.0,
            ),
            _raw_row(
                timestamp_ms=8,
                exchange_ms=7,
                totalvol=7.0,
                totalvalue=698.0,
                bid=100.0,
                ask=101.0,
                lastpx=99.0,
                openinterest=52.0,
            ),
        ]
    quoted = pd.DataFrame(quoted_rows)
    hedge_rows = [
        _raw_row(
            timestamp_ms=0,
            exchange_ms=0,
            totalvol=5.0,
            totalvalue=495.0,
            bid=99.0,
            ask=100.0,
            lastpx=99.0,
            openinterest=70.0,
        )
    ]
    if hedge_second_row:
        hedge_rows.append(
            _raw_row(
                timestamp_ms=4,
                exchange_ms=3,
                totalvol=8.0,
                totalvalue=792.0,
                bid=99.0,
                ask=100.0,
                lastpx=99.0,
                openinterest=71.0,
            )
        )
    hedge = pd.DataFrame(hedge_rows)
    return (
        RawSnapshotFrame("quoted-source", "Q", "sha256:" + "a" * 64, quoted),
        RawSnapshotFrame("hedge-source", "H", "sha256:" + "b" * 64, hedge),
    )


def _read_raw_sources(
    root: Path,
    *,
    sources: tuple[RawSnapshotFrame, RawSnapshotFrame] | None = None,
    config: RawSnapshotAdapterConfig | None = None,
):
    quoted, hedge = sources or _raw_sources()
    quoted_path = root / "quoted.csv"
    hedge_path = root / "hedge.csv"
    quoted.frame.to_csv(quoted_path, index=False)
    hedge.frame.to_csv(hedge_path, index=False)
    return read_raw_snapshot_market_data(
        (
            RawSnapshotFile(quoted.source_id, quoted_path, quoted.contract),
            RawSnapshotFile(hedge.source_id, hedge_path, hedge.contract),
        ),
        config or _raw_config(),
    )


def _replay_config(root: Path) -> tuple[ProductionReplayConfig, HedgePairRef, ExecutionModelRef]:
    calendar = SessionCalendar("raw-snapshot-utc", "UTC", eod_time=time(9, 0, 10))
    quoted = InstrumentSpec("Q", 1.0, 1.0, calendar, "fees", "roll")
    hedge = InstrumentSpec("H", 1.0, 1.0, calendar, "fees", "roll")
    pair = HedgePairRef("raw-snapshot-pair", "Q", "H", "calendar", "1.0.0")
    model = ExecutionModelConfig("raw-snapshot-depth", "1.0.0", 1.0)
    model_ref = ExecutionModelRef(model.model_id, model.version)
    trial = TrialDeclaration(
        "raw-snapshot-trial",
        "development",
        "calibration",
        "holdout",
        "freeze",
        "raw-snapshot-policy",
        pair,
        (model_ref,),
        ("strict-loader", "raw_snapshot_adapter_v1"),
    )
    return (
        ProductionReplayConfig(
            "raw-snapshot-replay",
            HedgeMappingSpec(pair, 1.0, 1.0),
            (quoted, hedge),
            (model,),
            model_ref,
            (CapacityEnvelope("raw-snapshot-quoted-cap", pair, "Q", 1),),
            root,
            date(2025, 1, 2),
            trial,
            {
                "market_data": "declared-raw-snapshot-fixture",
                "signal_data": "no-signals",
                "configuration": "raw-snapshot-config",
                "code": "raw-snapshot-code",
                "schema": "schema-v0.4",
                "fee_profile": "fees",
                "instrument_roll_mapping": "roll",
                "execution_models": "raw-snapshot-depth-v1",
            },
            max_execution_book_age_ms_by_product={"Q": 1_000.0, "H": 1_000.0},
        ),
        pair,
        model_ref,
    )


class _ResearchSnapshotMakerPolicy:
    def __init__(self, pair: HedgePairRef, model: ExecutionModelRef) -> None:
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
            "queue_ahead": 2.0 if not self._used else None,
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
            "raw-snapshot-maker",
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
        return PolicyProposal(
            MakerHedgeIntentBatch(maker_intent=maker, maker_capacity_envelope_id="raw-snapshot-quoted-cap"),
            attributes,
        )


def test_raw_snapshot_adapter_keeps_raw_fields_derives_buckets_and_assigns_global_sequences():
    market_data = adapt_raw_snapshot_frames(_raw_sources(), _raw_config())

    assert tuple(market_data.frame["source_seq"]) == (0, 1, 2)
    assert len(set(zip(market_data.frame["recv_ts"], market_data.frame["source_seq"]))) == 3
    assert {"timestamp", "exchtime", "lastpx", "openinterest"} <= set(market_data.frame.columns)
    interval = next(value for value in market_data.frame["snapshot_interval"] if isinstance(value, dict))
    assert interval["quantity"] == 3
    assert interval["buckets"] == [{"price": 99.0, "quantity": 2}, {"price": 100.0, "quantity": 1}]
    assert interval["raw_file_hash"] == "sha256:" + "a" * 64
    assert market_data.source_provenance is not None
    assert not market_data.source_provenance.source_bytes_authenticated
    assert market_data.source_provenance.as_provenance()["merge_order"] == "exchange_batch_then_contract_then_source_id_then_row_ordinal_v1"
    events = market_data.to_ingress_events(event_id_prefix="raw-adapter")
    interval_event = next(event for event in events if event.payload.get("snapshot_interval") is not None)
    assert interval_event.available_at == BASE_TS + timedelta(milliseconds=4)
    assert interval_event.payload["snapshot_interval"]["raw_row_ordinal"] == 1

    with TemporaryDirectory() as temporary:
        config, _, _ = _replay_config(Path(temporary))
        try:
            ProductionReplayAdapter(config)._require_snapshot_adapter_provenance(market_data)
        except FoundationContractError as exc:
            assert "authenticated raw_snapshot_adapter_v1 provenance" in str(exc)
        else:
            raise AssertionError("production must reject caller-asserted raw frame hashes")


def test_raw_snapshot_adapter_emits_proxy_intervals_only_for_the_declared_quoted_contract():
    sources = _raw_sources(hedge_second_row=True)
    market_data = adapt_raw_snapshot_frames(sources, _raw_config())

    quoted_intervals = tuple(
        value for value in market_data.frame.loc[market_data.frame["contract"] == "Q", "snapshot_interval"] if isinstance(value, dict)
    )
    hedge_intervals = tuple(market_data.frame.loc[market_data.frame["contract"] == "H", "snapshot_interval"])
    assert len(quoted_intervals) == 1
    assert hedge_intervals == (None, None)
    assert market_data.source_provenance is not None
    assert market_data.source_provenance.config.proxy_interval_contracts == ("Q",)

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        authenticated = _read_raw_sources(root, sources=sources)
        config, pair, model_ref = _replay_config(root / "artifacts")
        config = replace(
            config,
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.0, "H": 99.0},
                PnlAccountingView("accounting", 0.0),
                PnlAccountingView("cycle", 0.0),
            ),
            research_export=True,
        )
        result = ProductionReplayAdapter(config).run(authenticated, _ResearchSnapshotMakerPolicy(pair, model_ref))

    assert result.telemetry.eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible


def test_raw_snapshot_proxy_contract_declaration_is_validated_and_bound_to_the_quoted_product():
    for contracts in ((), ("Q", "Q"), ("X",)):
        try:
            _raw_config(proxy_interval_contracts=contracts)
        except FoundationContractError as exc:
            assert "proxy_interval_contracts" in str(exc)
        else:
            raise AssertionError("proxy interval contracts must be declared as a unique non-empty universe subset")

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        market_data = _read_raw_sources(
            root,
            sources=_raw_sources(hedge_second_row=True),
            config=_raw_config(proxy_interval_contracts=("H",)),
        )
        config, _, _ = _replay_config(root / "artifacts")
        try:
            ProductionReplayAdapter(config)._require_snapshot_adapter_provenance(market_data)
        except FoundationContractError as exc:
            assert "must exactly match the replay quoted product" in str(exc)
        else:
            raise AssertionError("production replay must bind proxy intervals to the quoted product")


def test_raw_snapshot_provenance_binds_bucket_configuration_and_matches_instrument_specs():
    baseline = adapt_raw_snapshot_frames(_raw_sources(), _raw_config())
    altered = adapt_raw_snapshot_frames(
        _raw_sources(),
        _raw_config(multiplier_by_contract={"Q": 1.01, "H": 1.0}),
    )
    baseline_interval = next(value for value in baseline.frame["snapshot_interval"] if isinstance(value, dict))
    altered_interval = next(value for value in altered.frame["snapshot_interval"] if isinstance(value, dict))

    assert baseline_interval["buckets"] == [{"price": 99.0, "quantity": 2}, {"price": 100.0, "quantity": 1}]
    assert altered_interval["buckets"] == [{"price": 98.0, "quantity": 2}, {"price": 99.0, "quantity": 1}]
    assert baseline.source_provenance is not None
    assert altered.source_provenance is not None
    assert baseline.source_provenance.as_provenance() != altered.source_provenance.as_provenance()
    assert baseline.source_provenance.as_provenance()["adapter_configuration"]["multiplier_by_contract"] == {
        "Q": 1.0,
        "H": 1.0,
    }

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        authenticated = _read_raw_sources(root)
        config, pair, _ = _replay_config(root / "artifacts")
        mismatched_quoted = InstrumentSpec(
            pair.quoted_product,
            2.0,
            1.0,
            config.instrument_specs[0].calendar,
            "fees",
            "roll",
        )
        mismatched_config = replace(config, instrument_specs=(mismatched_quoted, config.instrument_specs[1]))
        try:
            ProductionReplayAdapter(mismatched_config)._require_snapshot_adapter_provenance(authenticated)
        except FoundationContractError as exc:
            assert "tick/multiplier" in str(exc)
        else:
            raise AssertionError("production must bind adapter tables to InstrumentSpec values")


def test_raw_snapshot_adapter_rejects_off_tick_books_and_can_drop_an_invalid_interval():
    quoted, hedge = _raw_sources()
    off_tick = quoted.frame.copy()
    off_tick.loc[0, "bidpx0"] = 100.5
    try:
        adapt_raw_snapshot_frames(
            (RawSnapshotFrame(quoted.source_id, quoted.contract, quoted.raw_file_hash, off_tick), hedge),
            _raw_config(),
        )
    except FoundationContractError as exc:
        assert "off the declared tick grid" in str(exc)
    else:
        raise AssertionError("off-tick raw snapshot depth must fail before ingress")

    dropped = adapt_raw_snapshot_frames(
        _raw_sources(invalid_interval=True),
        _raw_config(invalid_interval_disposition="drop"),
    )
    assert dropped.accepted_rows == 2
    assert dropped.dropped_rows == 1
    assert dropped.issues == (
        LoaderValidationIssue(
            1,
            "invalid_snapshot_interval_volume",
            "drop",
            "raw snapshot interval volume must be a positive integer",
            "quoted-source",
            1,
        ),
    )
    assert not any(isinstance(value, dict) for value in dropped.frame["snapshot_interval"])


def test_raw_snapshot_adapter_rejects_or_drops_off_depth_interval_buckets_before_matching():
    sources = _raw_sources(off_depth_interval=True)
    try:
        adapt_raw_snapshot_frames(sources, _raw_config())
    except FoundationContractError as exc:
        assert "outside the retained five-level price boundary" in str(exc)
    else:
        raise AssertionError("an off-depth interval bucket must fail closed by default")

    dropped = adapt_raw_snapshot_frames(
        sources,
        _raw_config(off_depth_interval_disposition="drop"),
    )
    quoted_intervals = tuple(dropped.frame.loc[dropped.frame["contract"] == "Q", "snapshot_interval"])
    assert quoted_intervals == (None,)
    assert dropped.dropped_rows == 1
    assert dropped.issues == (
        LoaderValidationIssue(
            1,
            "off_depth_snapshot_interval_bucket",
            "drop",
            "raw snapshot interval bucket falls outside the retained five-level price boundary",
            "quoted-source",
            1,
        ),
    )
    assert dropped.source_provenance is not None
    assert dropped.source_provenance.config.off_depth_interval_disposition == "drop"

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        authenticated = _read_raw_sources(
            root,
            sources=_raw_sources(off_depth_interval=True, hedge_second_row=True),
            config=_raw_config(off_depth_interval_disposition="drop"),
        )
        config, pair, model_ref = _replay_config(root / "artifacts")
        config = replace(
            config,
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.0, "H": 99.0},
                PnlAccountingView("accounting", 0.0),
                PnlAccountingView("cycle", 0.0),
            ),
            research_export=True,
        )
        try:
            ProductionReplayAdapter(config).run(authenticated, _ResearchSnapshotMakerPolicy(pair, model_ref))
        except FoundationContractError as exc:
            assert "must contain exactly the declared book products" in str(exc)
        else:
            raise AssertionError("dropping one member of an exchange batch must fail closed in production replay")


def test_raw_snapshot_file_reader_hashes_declared_bytes_before_adaptation():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        market_data = _read_raw_sources(root)
        source_hashes = {
            item.source_id: item.raw_file_hash
            for item in market_data.source_provenance.sources
        }
        expected_hashes = {
            "quoted-source": "sha256:" + hashlib.sha256((root / "quoted.csv").read_bytes()).hexdigest(),
            "hedge-source": "sha256:" + hashlib.sha256((root / "hedge.csv").read_bytes()).hexdigest(),
        }

    assert source_hashes == expected_hashes
    assert market_data.source_provenance.source_bytes_authenticated
    events = market_data.to_ingress_events(event_id_prefix="raw-source-check")
    assert market_data.source_provenance.adapted_replay_hash == adapted_replay_events_hash(events)


def test_raw_snapshot_file_reader_parses_the_same_bytes_it_hashes():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        observed_sources = []
        original_read_csv = pd.read_csv

        def capture_read_csv(source, *args, **kwargs):
            observed_sources.append(source)
            return original_read_csv(source, *args, **kwargs)

        with patch("common.foundation_loader.pd.read_csv", side_effect=capture_read_csv):
            _read_raw_sources(root)

    assert len(observed_sources) == 2
    assert all(isinstance(source, BytesIO) for source in observed_sources)


def test_snapshot_intervals_mutated_after_authentication_fail_the_replay_payload_binding():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        market_data = _read_raw_sources(root, sources=_raw_sources(hedge_second_row=True))
        config, pair, model_ref = _replay_config(root / "artifacts")
        interval_row = next(index for index, value in market_data.frame["snapshot_interval"].items() if isinstance(value, dict))
        tampered = market_data.frame.copy(deep=True)
        interval = dict(tampered.at[interval_row, "snapshot_interval"])
        interval["quantity"] = 5
        interval["buckets"] = [{"price": 100.0, "quantity": 5}]
        tampered.at[interval_row, "snapshot_interval"] = interval
        try:
            ProductionReplayAdapter(config).run(
                replace(market_data, frame=tampered),
                _ResearchSnapshotMakerPolicy(pair, model_ref),
            )
        except FoundationContractError as exc:
            assert "adapted raw snapshot replay payload" in str(exc)
        else:
            raise AssertionError("a post-authentication interval mutation must fail before replay")


def test_raw_snapshot_reset_drop_starts_a_new_source_epoch_for_cumulative_validation():
    market_data = adapt_raw_snapshot_frames(
        _raw_sources(cumulative_reset=True),
        _raw_config(cumulative_reset_disposition="drop"),
    )

    assert market_data.accepted_rows == 3
    assert market_data.dropped_rows == 1
    assert market_data.issues == (
        LoaderValidationIssue(
            1,
            "raw_snapshot_cumulative_reset",
            "drop",
            "raw snapshot cumulative total reset",
            "quoted-source",
            1,
        ),
    )
    quoted_rows = market_data.frame.loc[market_data.frame["contract"] == "Q"]
    assert tuple(quoted_rows["raw_cumulative_epoch"]) == ("quoted-source:epoch:0", "quoted-source:epoch:1")
    interval = next(value for value in quoted_rows["snapshot_interval"] if isinstance(value, dict))
    assert interval["buckets"] == [{"price": 99.0, "quantity": 2}]


def test_snapshot_proxy_matcher_conserves_and_rejects_a_replayed_interval():
    fixture = make_dual_book_fixture()
    context = fixture.decision_context
    maker = OrderIntent(
        "snapshot-proxy-maker",
        context.run_id,
        context.decision_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        OrderRole.MAKER,
        OrderSide.BUY,
        1,
        78000.0,
    )
    matcher = PassiveMatchingService(object())
    matcher.register_intent(
        maker,
        context,
        queue_ahead_submit=0,
        arrival_book_snapshot=context.quoted_book,
    )
    interval = SnapshotInterval(
        "snapshot-interval-1",
        "quoted-source",
        "sha256:" + "c" * 64,
        3,
        context.run_id,
        fixture.hedge_pair,
        fixture.quoted_spec.product,
        context.dec_ts + timedelta(milliseconds=1),
        context.feed_seq,
        context.quoted_book,
        "snapshot_interval_queue_proxy_v1",
        "bid_then_ask_v1",
        1,
        (SnapshotIntervalPriceBucket(78000.0, 1),),
    )
    matches = matcher.match_snapshot_interval(interval)

    assert len(matches) == 1
    assert matches[0].fill_qty == matches[0].bucket_quantity == interval.quantity
    assert matches[0].interval_reference.endswith(":snapshot-interval-1")
    try:
        matcher.match_snapshot_interval(interval)
    except FoundationContractError as exc:
        assert "already been matched" in str(exc)
    else:
        raise AssertionError("a raw snapshot interval must not consume queue twice")


def test_raw_snapshot_replay_emits_separately_typed_proxy_evidence_and_research_fields():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        market_data = _read_raw_sources(root, sources=_raw_sources(hedge_second_row=True))
        config, pair, model_ref = _replay_config(root / "artifacts")
        config = replace(
            config,
            economic_inputs=EconomicReplayInputs(
                {"Q": 100.0, "H": 99.0},
                PnlAccountingView("accounting", 0.0),
                PnlAccountingView("cycle", 0.0),
            ),
            research_export=True,
        )
        result = ProductionReplayAdapter(config).run(
            market_data, _ResearchSnapshotMakerPolicy(pair, model_ref)
        )
        fills = tuple(load_canonical_table(root / "artifacts" / config.run_id, "fills"))
        research_path = root / "artifacts" / "research" / config.run_id / "tables" / "fills.jsonl"
        research_fills = tuple(pd.read_json(research_path, lines=True).to_dict("records"))
        quoted_hash = next(
            source.raw_file_hash for source in market_data.source_provenance.sources if source.source_id == "quoted-source"
        )

    proxy = next(row for row in fills if row["record_type"] == "snapshot_interval_proxy_evidence")
    maker_ledger = next(
        row for row in fills if row["record_type"] == "ledger_effect" and row.get("order_role") == "maker"
    )
    research_proxy = next(
        row for row in research_fills if row["match_evidence_type"] == "snapshot_interval_queue_proxy_v1"
    )
    assert result.telemetry.eligible
    assert result.research_telemetry is not None and result.research_telemetry.eligible
    assert "raw_snapshot_adapter" in result.telemetry.provenance.artifact_hashes
    assert "matched_trade_ref" not in proxy
    assert proxy["matched_interval_quantity"] == 3
    assert proxy["matched_interval_bucket_quantity"] == 1
    assert proxy["raw_file_hash"] == quoted_hash
    assert maker_ledger["matched_passive_fill_id"] == proxy["fill_id"]
    assert research_proxy["source_interval_quantity"] == 3
    assert research_proxy["source_interval_bucket_price"] == 100.0
    assert research_proxy["raw_file_id"] == "quoted-source"
    assert research_proxy["price_reach_rule"] == "bid_then_ask_v1"
