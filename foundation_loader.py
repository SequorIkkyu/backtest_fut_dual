"""Strict, ingress-ready market-data validation for supported foundation runs.

Historical ``backtest.load`` retains compatibility cleaning rules for old
strategies.  This module is the supported S0 loader: it never invents prices
for empty depth, selects no undeclared contracts, and reports every dropped or
rejected row through an explicit validation result before causal ingress.
"""

from __future__ import annotations

import io
import math
import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from common.foundation_contracts import FoundationContractError, IngressEvent, IngressKind


_RAW_SNAPSHOT_ADAPTER_NAME = "raw_snapshot_adapter_v1"
_RAW_SNAPSHOT_FILE_CONTENT_HASH_AUTHORITY = "declared_raw_file_content_sha256_v1"
_RAW_SNAPSHOT_CALLER_HASH_AUTHORITY = "caller_asserted_in_memory_hash_v1"
_RAW_SNAPSHOT_FILE_CONTENT_AUTHORITY = object()


@dataclass(frozen=True)
class RawSnapshotFile:
    """One declared raw snapshot file and its destination contract."""

    source_id: str
    path: str | Path
    contract: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "contract"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{field_name} must be a non-empty string")
        if not isinstance(self.path, (str, Path)) or not str(self.path).strip():
            raise FoundationContractError("path must be a non-empty path")


@dataclass(frozen=True)
class RawSnapshotFrame:
    """In-memory raw snapshot source used by deterministic adapters and tests."""

    source_id: str
    contract: str
    raw_file_hash: str
    frame: pd.DataFrame

    def __post_init__(self) -> None:
        for field_name in ("source_id", "contract", "raw_file_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{field_name} must be a non-empty string")
        if not self.raw_file_hash.startswith("sha256:") or len(self.raw_file_hash) != 71:
            raise FoundationContractError("raw_file_hash must be a sha256 digest")
        if not isinstance(self.frame, pd.DataFrame) or self.frame.empty:
            raise FoundationContractError("raw snapshot frame must be a non-empty DataFrame")


@dataclass(frozen=True)
class RawSnapshotSourceProvenance:
    """Immutable identity and row extent for one declared raw snapshot file."""

    source_id: str
    contract: str
    raw_file_hash: str
    rows: int

    def __post_init__(self) -> None:
        for field_name in ("source_id", "contract", "raw_file_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{field_name} must be a non-empty string")
        if not self.raw_file_hash.startswith("sha256:") or len(self.raw_file_hash) != 71:
            raise FoundationContractError("raw_file_hash must be a sha256 digest")
        if not isinstance(self.rows, int) or self.rows <= 0:
            raise FoundationContractError("rows must be a positive integer")

    def as_provenance(self) -> Mapping[str, Any]:
        """Return the canonical serializable source-manifest entry."""
        return {
            "source_id": self.source_id,
            "contract": self.contract,
            "raw_file_hash": self.raw_file_hash,
            "rows": self.rows,
        }


@dataclass(frozen=True)
class RawSnapshotAdapterConfig:
    """Versioned conversion policy for five-level cumulative-flow snapshots."""

    declared_contract_universe: tuple[str, ...]
    proxy_interval_contracts: tuple[str, ...]
    source_timezone: str
    tick_by_contract: Mapping[str, float]
    multiplier_by_contract: Mapping[str, float]
    model_version: str = "snapshot_interval_queue_proxy_v1"
    price_reach_rule: str = "bid_then_ask_v1"
    availability_convention: str = "max_exchange_timestamp_v1"
    first_row_disposition: str = "no_interval"
    zero_volume_disposition: str = "no_interval"
    cumulative_reset_disposition: str = "reject"
    invalid_interval_disposition: str = "reject"
    off_depth_interval_disposition: str = "reject"

    def __post_init__(self) -> None:
        if not isinstance(self.declared_contract_universe, tuple) or not self.declared_contract_universe:
            raise FoundationContractError("declared_contract_universe must be a non-empty tuple")
        if any(not isinstance(value, str) or not value.strip() for value in self.declared_contract_universe):
            raise FoundationContractError("declared_contract_universe must contain non-empty strings")
        if len(set(self.declared_contract_universe)) != len(self.declared_contract_universe):
            raise FoundationContractError("declared_contract_universe must not contain duplicates")
        if not isinstance(self.proxy_interval_contracts, tuple) or not self.proxy_interval_contracts:
            raise FoundationContractError("proxy_interval_contracts must be a non-empty tuple")
        if any(not isinstance(value, str) or not value.strip() for value in self.proxy_interval_contracts):
            raise FoundationContractError("proxy_interval_contracts must contain non-empty strings")
        if len(set(self.proxy_interval_contracts)) != len(self.proxy_interval_contracts):
            raise FoundationContractError("proxy_interval_contracts must not contain duplicates")
        if not set(self.proxy_interval_contracts) <= set(self.declared_contract_universe):
            raise FoundationContractError("proxy_interval_contracts must be a subset of declared_contract_universe")
        try:
            ZoneInfo(self.source_timezone)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise FoundationContractError("source_timezone must be an available IANA timezone") from exc
        normalized_tables: dict[str, Mapping[str, float]] = {}
        for name, values in (("tick_by_contract", self.tick_by_contract), ("multiplier_by_contract", self.multiplier_by_contract)):
            if not isinstance(values, Mapping) or set(values) != set(self.declared_contract_universe):
                raise FoundationContractError(f"{name} must cover exactly declared_contract_universe")
            normalized: dict[str, float] = {}
            for contract, value in values.items():
                if not isinstance(contract, str) or not math.isfinite(float(value)) or float(value) <= 0:
                    raise FoundationContractError(f"{name} values must be finite and positive")
                normalized[contract] = float(value)
            normalized_tables[name] = MappingProxyType(
                {contract: normalized[contract] for contract in self.declared_contract_universe}
            )
        object.__setattr__(self, "tick_by_contract", normalized_tables["tick_by_contract"])
        object.__setattr__(self, "multiplier_by_contract", normalized_tables["multiplier_by_contract"])
        for name in ("model_version", "price_reach_rule", "availability_convention"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{name} must be a non-empty string")
        if self.price_reach_rule != "bid_then_ask_v1":
            raise FoundationContractError("unsupported raw snapshot price_reach_rule")
        if self.availability_convention != "max_exchange_timestamp_v1":
            raise FoundationContractError("unsupported raw snapshot availability_convention")
        if self.first_row_disposition != "no_interval":
            raise FoundationContractError("first_row_disposition must be 'no_interval'")
        if self.zero_volume_disposition not in {"no_interval", "reject", "drop"}:
            raise FoundationContractError("zero_volume_disposition must be no_interval, reject, or drop")
        for name in ("cumulative_reset_disposition", "invalid_interval_disposition", "off_depth_interval_disposition"):
            if getattr(self, name) not in {"reject", "drop"}:
                raise FoundationContractError(f"{name} must be reject or drop")

    @property
    def market_data_config(self) -> "MarketDataValidationConfig":
        return MarketDataValidationConfig(
            self.declared_contract_universe,
            book_levels=5,
            source_timezone=self.source_timezone,
            missing_data_disposition="reject",
            invalid_book_disposition="reject",
            zero_depth_disposition="reject",
            volume_correction_disposition="reject",
            cumulative_epoch_column="raw_cumulative_epoch",
        )

    def as_provenance(self) -> Mapping[str, Any]:
        """Return every value that controls raw snapshot adaptation."""
        return {
            "declared_contract_universe": self.declared_contract_universe,
            "proxy_interval_contracts": self.proxy_interval_contracts,
            "source_timezone": self.source_timezone,
            "tick_by_contract": dict(self.tick_by_contract),
            "multiplier_by_contract": dict(self.multiplier_by_contract),
            "model_version": self.model_version,
            "price_reach_rule": self.price_reach_rule,
            "availability_convention": self.availability_convention,
            "first_row_disposition": self.first_row_disposition,
            "zero_volume_disposition": self.zero_volume_disposition,
            "cumulative_reset_disposition": self.cumulative_reset_disposition,
            "invalid_interval_disposition": self.invalid_interval_disposition,
            "off_depth_interval_disposition": self.off_depth_interval_disposition,
            "book_levels": 5,
        }


@dataclass(frozen=True)
class RawSnapshotAdapterProvenance:
    """Typed, immutable authority for raw sources and their replay payload."""

    config: RawSnapshotAdapterConfig
    sources: tuple[RawSnapshotSourceProvenance, ...]
    source_hash_authority: str
    adapted_replay_hash: str
    _source_bytes_authority: object | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.config, RawSnapshotAdapterConfig):
            raise FoundationContractError("config must be a RawSnapshotAdapterConfig")
        if not isinstance(self.sources, tuple) or not self.sources or any(
            not isinstance(source, RawSnapshotSourceProvenance) for source in self.sources
        ):
            raise FoundationContractError("sources must be a non-empty tuple of RawSnapshotSourceProvenance values")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise FoundationContractError("raw snapshot provenance source_id values must be unique")
        if {source.contract for source in self.sources} != set(self.config.declared_contract_universe):
            raise FoundationContractError("raw snapshot provenance sources must cover exactly declared_contract_universe")
        if self.source_hash_authority not in {
            _RAW_SNAPSHOT_FILE_CONTENT_HASH_AUTHORITY,
            _RAW_SNAPSHOT_CALLER_HASH_AUTHORITY,
        }:
            raise FoundationContractError("raw snapshot source_hash_authority is unsupported")
        if not isinstance(self.adapted_replay_hash, str) or not self.adapted_replay_hash.startswith("sha256:") or len(
            self.adapted_replay_hash
        ) != 71:
            raise FoundationContractError("adapted_replay_hash must be a sha256 digest")

    @classmethod
    def _from_adapter(
        cls,
        config: RawSnapshotAdapterConfig,
        sources: tuple[RawSnapshotSourceProvenance, ...],
        *,
        source_bytes_authenticated: bool,
        adapted_replay_hash: str,
    ) -> "RawSnapshotAdapterProvenance":
        """Construct provenance only at the loader boundary that owns the source bytes."""
        authority = (
            _RAW_SNAPSHOT_FILE_CONTENT_HASH_AUTHORITY
            if source_bytes_authenticated
            else _RAW_SNAPSHOT_CALLER_HASH_AUTHORITY
        )
        result = cls(config, sources, authority, adapted_replay_hash)
        if source_bytes_authenticated:
            object.__setattr__(result, "_source_bytes_authority", _RAW_SNAPSHOT_FILE_CONTENT_AUTHORITY)
        return result

    @property
    def source_bytes_authenticated(self) -> bool:
        """Whether the hash was computed from declared file bytes in this loader."""
        return (
            self.source_hash_authority == _RAW_SNAPSHOT_FILE_CONTENT_HASH_AUTHORITY
            and self._source_bytes_authority is _RAW_SNAPSHOT_FILE_CONTENT_AUTHORITY
        )

    def as_provenance(self) -> Mapping[str, Any]:
        """Return the complete canonical provenance artifact for telemetry."""
        return {
            "adapter": _RAW_SNAPSHOT_ADAPTER_NAME,
            "adapter_configuration": self.config.as_provenance(),
            "sources": tuple(source.as_provenance() for source in self.sources),
            "merge_order": "exchange_batch_then_contract_then_source_id_then_row_ordinal_v1",
            "source_hash_authority": self.source_hash_authority,
            "adapted_replay_hash": self.adapted_replay_hash,
        }


@dataclass(frozen=True)
class MarketDataValidationConfig:
    """Declared universe and failure dispositions for one foundation loader run."""

    declared_contract_universe: tuple[str, ...]
    book_levels: int = 1
    source_timezone: str | None = None
    missing_data_disposition: str = "reject"
    invalid_book_disposition: str = "reject"
    zero_depth_disposition: str = "reject"
    volume_correction_disposition: str = "reject"
    cumulative_volume_columns: tuple[str, ...] = ("totalvol", "totalvalue")
    cumulative_epoch_column: str | None = None
    exchange_batch_id_column: str | None = None
    exchange_batch_seq_column: str | None = None
    require_complete_exchange_batches: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.declared_contract_universe, tuple) or not self.declared_contract_universe:
            raise FoundationContractError("declared_contract_universe must be a non-empty tuple")
        if any(not isinstance(contract, str) or not contract.strip() for contract in self.declared_contract_universe):
            raise FoundationContractError("declared_contract_universe must contain non-empty strings")
        if len(set(self.declared_contract_universe)) != len(self.declared_contract_universe):
            raise FoundationContractError("declared_contract_universe must not contain duplicates")
        if not isinstance(self.book_levels, int) or self.book_levels <= 0:
            raise FoundationContractError("book_levels must be a positive integer")
        if self.source_timezone is not None:
            if not isinstance(self.source_timezone, str) or not self.source_timezone.strip():
                raise FoundationContractError("source_timezone must be a non-empty IANA timezone or None")
            try:
                ZoneInfo(self.source_timezone)
            except ZoneInfoNotFoundError as exc:
                raise FoundationContractError("source_timezone is not available") from exc
        for field_name in (
            "missing_data_disposition",
            "invalid_book_disposition",
            "zero_depth_disposition",
            "volume_correction_disposition",
        ):
            if getattr(self, field_name) not in {"reject", "drop"}:
                raise FoundationContractError(f"{field_name} must be 'reject' or 'drop'")
        if not isinstance(self.cumulative_volume_columns, tuple) or any(
            not isinstance(column, str) or not column.strip() for column in self.cumulative_volume_columns
        ):
            raise FoundationContractError("cumulative_volume_columns must be a tuple of non-empty strings")
        if self.cumulative_epoch_column is not None and (
            not isinstance(self.cumulative_epoch_column, str) or not self.cumulative_epoch_column.strip()
        ):
            raise FoundationContractError("cumulative_epoch_column must be a non-empty string or None")
        for field_name in ("exchange_batch_id_column", "exchange_batch_seq_column"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise FoundationContractError(f"{field_name} must be a non-empty string or None")
        if not isinstance(self.require_complete_exchange_batches, bool):
            raise FoundationContractError("require_complete_exchange_batches must be a bool")

    @property
    def required_columns(self) -> tuple[str, ...]:
        book_columns = tuple(
            column for level in range(self.book_levels) for column in (f"bidpx{level}", f"bidvol{level}", f"askpx{level}", f"askvol{level}")
        )
        epoch_column = (self.cumulative_epoch_column,) if self.cumulative_epoch_column is not None else ()
        batch_columns = tuple(
            column
            for column in (self.exchange_batch_id_column, self.exchange_batch_seq_column)
            if column is not None
        )
        return ("contract", "exchange_ts", "recv_ts", "source_seq", *book_columns, *self.cumulative_volume_columns, *epoch_column, *batch_columns)


@dataclass(frozen=True)
class LoaderValidationIssue:
    """One deterministic row-level validation disposition."""

    row_number: int
    code: str
    disposition: str
    message: str
    raw_source_id: str | None = None
    raw_row_ordinal: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.row_number, int) or self.row_number < 0:
            raise FoundationContractError("row_number must be a non-negative integer")
        for field_name in ("code", "disposition", "message"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise FoundationContractError(f"{field_name} must be a non-empty string")
        if self.disposition not in {"drop", "reject"}:
            raise FoundationContractError("validation issue disposition must be 'drop' or 'reject'")
        if (self.raw_source_id is None) != (self.raw_row_ordinal is None):
            raise FoundationContractError("raw_source_id and raw_row_ordinal must be provided together")
        if self.raw_source_id is not None and (not isinstance(self.raw_source_id, str) or not self.raw_source_id.strip()):
            raise FoundationContractError("raw_source_id must be a non-empty string or None")
        if self.raw_row_ordinal is not None and (
            not isinstance(self.raw_row_ordinal, int) or self.raw_row_ordinal < 0
        ):
            raise FoundationContractError("raw_row_ordinal must be a non-negative integer or None")


@dataclass(frozen=True)
class ValidatedMarketData:
    """Validated rows plus all explicit dropped-row dispositions."""

    frame: pd.DataFrame
    config: MarketDataValidationConfig
    issues: tuple[LoaderValidationIssue, ...] = ()
    source_provenance: RawSnapshotAdapterProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.frame, pd.DataFrame):
            raise FoundationContractError("frame must be a pandas DataFrame")
        if not isinstance(self.config, MarketDataValidationConfig):
            raise FoundationContractError("config must be a MarketDataValidationConfig")
        if not isinstance(self.issues, tuple) or any(not isinstance(issue, LoaderValidationIssue) for issue in self.issues):
            raise FoundationContractError("issues must be a tuple of LoaderValidationIssue values")
        if self.source_provenance is not None and not isinstance(self.source_provenance, RawSnapshotAdapterProvenance):
            raise FoundationContractError("source_provenance must be a RawSnapshotAdapterProvenance or None")

    @property
    def accepted_rows(self) -> int:
        return len(self.frame)

    @property
    def dropped_rows(self) -> int:
        return sum(issue.disposition == "drop" for issue in self.issues)

    def to_ingress_events(self, *, event_id_prefix: str) -> tuple[IngressEvent, ...]:
        """Convert accepted canonical rows to immutable, causal book ingress events."""
        if not isinstance(event_id_prefix, str) or not event_id_prefix.strip():
            raise FoundationContractError("event_id_prefix must be a non-empty string")
        events: list[IngressEvent] = []
        for row_ordinal, (_, row) in enumerate(self.frame.iterrows()):
            event_values = _adapted_replay_event_values(row, self.config)
            events.append(
                IngressEvent(
                    f"{event_id_prefix}:{row_ordinal}:{event_values['source_seq']}",
                    event_values["product"],
                    IngressKind.BOOK,
                    event_values["exchange_ts"],
                    event_values["recv_ts"],
                    event_values["source_seq"],
                    event_values["payload"],
                    exchange_batch_id=event_values["exchange_batch_id"],
                    exchange_batch_seq=event_values["exchange_batch_seq"],
                )
            )
        return tuple(events)


def adapted_replay_events_hash(events: tuple[IngressEvent, ...]) -> str:
    """Hash the immutable book-event payload that a raw adapter authorizes.

    Event IDs are deliberately excluded: the replay run ID prefixes them after
    adaptation.  Every value consumed by ``CausalIngress`` is included.
    """
    if not isinstance(events, tuple) or any(not isinstance(event, IngressEvent) for event in events):
        raise FoundationContractError("adapted replay events must be a tuple of IngressEvent values")
    if any(event.kind is not IngressKind.BOOK for event in events):
        raise FoundationContractError("adapted replay events must contain only BOOK events")
    return _adapted_replay_hash(
        tuple(
            {
                "product": event.product,
                "exchange_ts": event.exchange_ts,
                "recv_ts": event.recv_ts,
                "source_seq": event.source_seq,
                "exchange_batch_id": event.exchange_batch_id,
                "exchange_batch_seq": event.exchange_batch_seq,
                "payload": event.payload,
            }
            for event in events
        )
    )


def _adapted_replay_frame_hash(frame: pd.DataFrame, config: MarketDataValidationConfig) -> str:
    """Hash the exact canonical rows that ``to_ingress_events`` will replay."""
    if not isinstance(frame, pd.DataFrame) or not isinstance(config, MarketDataValidationConfig):
        raise FoundationContractError("adapted replay hash requires validated market data")
    return _adapted_replay_hash(
        tuple(
            _adapted_replay_event_values(row, config)
            for _, row in frame.iterrows()
        )
    )


def _adapted_replay_event_values(row: Mapping[str, Any], config: MarketDataValidationConfig) -> dict[str, Any]:
    """Build the one canonical event projection used by hashing and ingress."""
    bids = [
        {"price": float(row[f"bidpx{level}"]), "quantity": int(row[f"bidvol{level}"])}
        for level in range(config.book_levels)
        if int(row[f"bidvol{level}"]) > 0
    ]
    asks = [
        {"price": float(row[f"askpx{level}"]), "quantity": int(row[f"askvol{level}"])}
        for level in range(config.book_levels)
        if int(row[f"askvol{level}"]) > 0
    ]
    payload: dict[str, Any] = {"bids": bids, "asks": asks}
    if "passive_trades" in row:
        payload["passive_trades"] = row["passive_trades"]
    if "snapshot_interval" in row and isinstance(row["snapshot_interval"], Mapping):
        payload["snapshot_interval"] = dict(row["snapshot_interval"])
    return {
        "product": str(row["contract"]),
        "exchange_ts": pd.Timestamp(row["exchange_ts"]).to_pydatetime(),
        "recv_ts": pd.Timestamp(row["recv_ts"]).to_pydatetime(),
        "source_seq": int(row["source_seq"]),
        "exchange_batch_id": str(row["exchange_batch_id"]),
        "exchange_batch_seq": int(row["exchange_batch_seq"]),
        "payload": payload,
    }


def _adapted_replay_hash(value: Any) -> str:
    """Encode a replay payload deterministically before binding it to provenance."""
    try:
        payload = json.dumps(
            _canonical_adapted_value(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FoundationContractError("adapted replay payload must be canonical JSON data") from exc
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_adapted_value(value: Any) -> Any:
    """Normalize the frozen ingress payload without accepting non-finite data."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise FoundationContractError("adapted replay timestamps must be timezone-aware")
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FoundationContractError("adapted replay mapping keys must be strings")
            normalized[key] = _canonical_adapted_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_adapted_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FoundationContractError("adapted replay floats must be finite")
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _canonical_adapted_value(item())
    raise FoundationContractError(f"adapted replay value is not canonicalizable: {type(value).__name__}")


def read_validated_market_data(path: str | Path, config: MarketDataValidationConfig) -> ValidatedMarketData:
    """Read one CSV then apply the exact declared validation contract."""
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError) as exc:
        raise FoundationContractError(f"market-data CSV cannot be read: {path}") from exc
    return validate_market_data(frame, config)


def read_raw_snapshot_market_data(
    sources: tuple[RawSnapshotFile, ...], config: RawSnapshotAdapterConfig
) -> ValidatedMarketData:
    """Read declared raw files and adapt their five-level snapshot semantics.

    Each file is parsed from the same byte sequence that is content-hashed, so
    the adapter's provenance binds the actual source payload rather than a
    second path read.
    """
    if not isinstance(sources, tuple) or not sources or any(not isinstance(source, RawSnapshotFile) for source in sources):
        raise FoundationContractError("sources must be a non-empty tuple of RawSnapshotFile")
    frames: list[RawSnapshotFrame] = []
    for source in sources:
        if source.contract not in config.declared_contract_universe:
            raise FoundationContractError("raw snapshot source contract is outside declared_contract_universe")
        try:
            raw_bytes = Path(source.path).read_bytes()
            frame = pd.read_csv(io.BytesIO(raw_bytes))
        except (OSError, ValueError) as exc:
            raise FoundationContractError(f"raw snapshot CSV cannot be read: {source.path}") from exc
        frames.append(
            RawSnapshotFrame(
                source.source_id,
                source.contract,
                f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}",
                frame,
            )
        )
    return _adapt_raw_snapshot_frames(tuple(frames), config, source_bytes_authenticated=True)


def adapt_raw_snapshot_frames(
    sources: tuple[RawSnapshotFrame, ...], config: RawSnapshotAdapterConfig
) -> ValidatedMarketData:
    """Normalize in-memory raw snapshots for deterministic adapter tests.

    Hashes supplied by this helper are caller assertions. The returned data is
    deliberately rejected by production replay whenever it carries an interval;
    use :func:`read_raw_snapshot_market_data` for production evidence.
    """
    return _adapt_raw_snapshot_frames(sources, config, source_bytes_authenticated=False)


def _adapt_raw_snapshot_frames(
    sources: tuple[RawSnapshotFrame, ...],
    config: RawSnapshotAdapterConfig,
    *,
    source_bytes_authenticated: bool,
) -> ValidatedMarketData:
    """Normalize declared raw snapshot frames without mutating their source fields."""
    if not isinstance(sources, tuple) or not sources or any(not isinstance(source, RawSnapshotFrame) for source in sources):
        raise FoundationContractError("sources must be a non-empty tuple of RawSnapshotFrame")
    if not isinstance(config, RawSnapshotAdapterConfig):
        raise FoundationContractError("config must be a RawSnapshotAdapterConfig")
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise FoundationContractError("raw snapshot source_id values must be unique")
    if {source.contract for source in sources} != set(config.declared_contract_universe):
        raise FoundationContractError("raw snapshot sources must cover exactly declared_contract_universe")

    canonical_rows: list[dict[str, Any]] = []
    raw_issues: list[LoaderValidationIssue] = []
    source_manifest: list[RawSnapshotSourceProvenance] = []
    required = {
        "timestamp",
        "exchtime",
        "totalvol",
        "totalvalue",
        *(f"{side}{field}{level}" for level in range(5) for side in ("bid", "ask") for field in ("px", "vol")),
    }
    for source in sources:
        if source.contract not in config.declared_contract_universe:
            raise FoundationContractError("raw snapshot source contract is outside declared_contract_universe")
        missing = sorted(required - set(source.frame.columns))
        if missing:
            raise FoundationContractError(f"raw snapshot source is missing required columns: {missing}")
        source_manifest.append(
            RawSnapshotSourceProvenance(
                source.source_id,
                source.contract,
                source.raw_file_hash,
                len(source.frame),
            )
        )
        previous_volume: float | None = None
        previous_value: float | None = None
        cumulative_epoch = 0
        for row_ordinal, (_, raw_row) in enumerate(source.frame.iterrows()):
            row = raw_row.to_dict()
            exchange_ts = _timestamp(row["exchtime"], config.source_timezone)
            source_ts = _timestamp(row["timestamp"], config.source_timezone)
            if exchange_ts is None or source_ts is None:
                _raw_disposition(
                    raw_issues,
                    source,
                    row_ordinal,
                    config.invalid_interval_disposition,
                    "invalid_raw_snapshot_timestamp",
                    "raw snapshot timestamps must be valid",
                )
                continue
            total_volume = _finite_number(row["totalvol"])
            total_value = _finite_number(row["totalvalue"])
            if total_volume is None or total_value is None or total_volume < 0 or total_value < 0:
                _raw_disposition(
                    raw_issues,
                    source,
                    row_ordinal,
                    config.invalid_interval_disposition,
                    "invalid_raw_snapshot_cumulative_totals",
                    "raw snapshot cumulative totals must be finite and non-negative",
                )
                continue
            _validate_raw_book_ticks(row, source.contract, config)
            interval: Mapping[str, Any] | None = None
            if previous_volume is not None and previous_value is not None:
                delta_volume = total_volume - previous_volume
                delta_value = total_value - previous_value
                if delta_volume < 0 or delta_value < 0:
                    _raw_disposition(
                        raw_issues,
                        source,
                        row_ordinal,
                        config.cumulative_reset_disposition,
                        "raw_snapshot_cumulative_reset",
                        "raw snapshot cumulative total reset",
                    )
                    cumulative_epoch += 1
                    previous_volume, previous_value = total_volume, total_value
                    continue
                if delta_volume == 0:
                    if delta_value != 0 or config.zero_volume_disposition == "reject":
                        _raw_disposition(
                            raw_issues,
                            source,
                            row_ordinal,
                            "reject",
                            "invalid_zero_volume_interval",
                            "zero-volume raw snapshot interval has invalid turnover",
                        )
                    if config.zero_volume_disposition == "drop":
                        _raw_disposition(
                            raw_issues,
                            source,
                            row_ordinal,
                            "drop",
                            "zero_volume_interval",
                            "zero-volume raw snapshot interval follows the declared drop disposition",
                        )
                        previous_volume, previous_value = total_volume, total_value
                        continue
                elif source.contract in config.proxy_interval_contracts:
                    interval = _snapshot_interval_payload(
                        source,
                        row_ordinal,
                        row,
                        exchange_ts,
                        source_ts,
                        delta_volume,
                        delta_value,
                        config,
                        raw_issues,
                    )
                    if interval is None:
                        previous_volume, previous_value = total_volume, total_value
                        continue
            previous_volume, previous_value = total_volume, total_value
            # Preserve every source field alongside the canonical fields.  The
            # raw file hash and ordinal remain the durable authority, while
            # keeping these values makes the adaptation inspectable without a
            # lossy CSV rewrite.
            normalized: dict[str, Any] = dict(row)
            normalized.update({
                "contract": source.contract,
                "exchange_ts": exchange_ts,
                "recv_ts": max(exchange_ts, source_ts),
                "exchange_batch_id": f"exchange-ts:{exchange_ts.isoformat()}",
                "exchange_batch_seq": None,
                "raw_source_id": source.source_id,
                "raw_row_ordinal": row_ordinal,
                "raw_file_hash": source.raw_file_hash,
                "raw_timestamp": source_ts.isoformat(),
                "raw_exchtime": exchange_ts.isoformat(),
                "totalvol": total_volume,
                "totalvalue": total_value,
                "raw_cumulative_epoch": f"{source.source_id}:epoch:{cumulative_epoch}",
                "snapshot_interval": interval,
            })
            for level in range(5):
                for side in ("bid", "ask"):
                    normalized[f"{side}px{level}"] = row[f"{side}px{level}"]
                    normalized[f"{side}vol{level}"] = row[f"{side}vol{level}"]
            canonical_rows.append(normalized)

    if not canonical_rows:
        raise FoundationContractError("raw snapshot adaptation accepted no rows")
    canonical_rows.sort(key=lambda row: (row["exchange_ts"], row["contract"], row["raw_source_id"], row["raw_row_ordinal"]))
    batch_sequence_by_timestamp: dict[pd.Timestamp, int] = {}
    for row in canonical_rows:
        timestamp = pd.Timestamp(row["exchange_ts"])
        row["exchange_batch_seq"] = batch_sequence_by_timestamp.setdefault(timestamp, len(batch_sequence_by_timestamp))
    for source_seq, row in enumerate(canonical_rows):
        row["source_seq"] = source_seq
    frame = pd.DataFrame(canonical_rows)
    validated = validate_market_data(frame, config.market_data_config)
    validated_issues = tuple(_with_raw_source_identity(issue, frame) for issue in validated.issues)
    provenance = RawSnapshotAdapterProvenance._from_adapter(
        config,
        tuple(source_manifest),
        source_bytes_authenticated=source_bytes_authenticated,
        adapted_replay_hash=_adapted_replay_frame_hash(validated.frame, validated.config),
    )
    return ValidatedMarketData(validated.frame, validated.config, tuple(raw_issues) + validated_issues, provenance)


def _snapshot_interval_payload(
    source: RawSnapshotFrame,
    row_ordinal: int,
    raw_row: Mapping[str, Any],
    exchange_ts: pd.Timestamp,
    source_ts: pd.Timestamp,
    delta_volume: float,
    delta_value: float,
    config: RawSnapshotAdapterConfig,
    issues: list[LoaderValidationIssue],
) -> Mapping[str, Any] | None:
    quantity = _integer(delta_volume)
    if quantity is None or quantity <= 0:
        _raw_disposition(
            issues,
            source,
            row_ordinal,
            config.invalid_interval_disposition,
            "invalid_snapshot_interval_volume",
            "raw snapshot interval volume must be a positive integer",
        )
        return None
    multiplier = float(config.multiplier_by_contract[source.contract])
    tick = float(config.tick_by_contract[source.contract])
    vwap = delta_value / (quantity * multiplier)
    if not math.isfinite(vwap) or vwap <= 0:
        _raw_disposition(
            issues,
            source,
            row_ordinal,
            config.invalid_interval_disposition,
            "invalid_snapshot_interval_vwap",
            "raw snapshot interval VWAP must be finite and positive",
        )
        return None
    lower_units = math.floor((vwap / tick) + 1e-12)
    lower = round(lower_units * tick, 12)
    upper = round((lower_units + 1) * tick, 12)
    upper_quantity = int(math.floor((((vwap - lower) / tick) * quantity) + 0.5))
    upper_quantity = max(0, min(quantity, upper_quantity))
    buckets: list[dict[str, Any]] = []
    if quantity - upper_quantity:
        buckets.append({"price": lower, "quantity": quantity - upper_quantity})
    if upper_quantity:
        buckets.append({"price": upper, "quantity": upper_quantity})
    depth_envelope = _retained_depth_price_envelope(raw_row, config, issues, source, row_ordinal)
    if depth_envelope is None:
        return None
    depth_floor, depth_ceiling = depth_envelope
    if any(bucket["price"] < depth_floor - tick / 10.0 or bucket["price"] > depth_ceiling + tick / 10.0 for bucket in buckets):
        _raw_disposition(
            issues,
            source,
            row_ordinal,
            config.off_depth_interval_disposition,
            "off_depth_snapshot_interval_bucket",
            "raw snapshot interval bucket falls outside the retained five-level price boundary",
        )
        return None
    return {
        "interval_id": f"{source.source_id}:row:{row_ordinal}",
        "raw_file_id": source.source_id,
        "raw_file_hash": source.raw_file_hash,
        "raw_row_ordinal": row_ordinal,
        "model_version": config.model_version,
        "price_reach_rule": config.price_reach_rule,
        "availability_convention": config.availability_convention,
        "quantity": quantity,
        "buckets": buckets,
        "raw_timestamp": source_ts.isoformat(),
        "raw_exchtime": exchange_ts.isoformat(),
    }


def _retained_depth_price_envelope(
    raw_row: Mapping[str, Any],
    config: RawSnapshotAdapterConfig,
    issues: list[LoaderValidationIssue],
    source: RawSnapshotFrame,
    row_ordinal: int,
) -> tuple[float, float] | None:
    """Return the executable price envelope of the current retained snapshot."""
    bids: list[float] = []
    asks: list[float] = []
    for level in range(5):
        bid_price = _finite_number(raw_row[f"bidpx{level}"])
        ask_price = _finite_number(raw_row[f"askpx{level}"])
        bid_quantity = _integer(raw_row[f"bidvol{level}"])
        ask_quantity = _integer(raw_row[f"askvol{level}"])
        if (
            bid_price is None
            or ask_price is None
            or bid_price <= 0
            or ask_price <= 0
            or bid_quantity is None
            or ask_quantity is None
        ):
            _raw_disposition(
                issues,
                source,
                row_ordinal,
                config.invalid_interval_disposition,
                "invalid_snapshot_interval_depth",
                "raw snapshot interval requires valid retained depth",
            )
            return None
        if bid_quantity > 0:
            bids.append(bid_price)
        if ask_quantity > 0:
            asks.append(ask_price)
    if not bids or not asks:
        _raw_disposition(
            issues,
            source,
            row_ordinal,
            config.invalid_interval_disposition,
            "empty_snapshot_interval_depth",
            "raw snapshot interval requires non-empty retained depth",
        )
        return None
    return min(bids), max(asks)


def _raw_disposition(
    issues: list[LoaderValidationIssue],
    source: RawSnapshotFrame,
    row_ordinal: int,
    disposition: str,
    code: str,
    message: str,
) -> None:
    issues.append(LoaderValidationIssue(row_ordinal, code, disposition, message, source.source_id, row_ordinal))
    if disposition == "reject":
        raise FoundationContractError(f"raw snapshot adapter rejected {source.source_id}:row:{row_ordinal}: {code}: {message}")
    if disposition != "drop":
        raise FoundationContractError("raw snapshot disposition must reject or drop")


def _with_raw_source_identity(issue: LoaderValidationIssue, frame: pd.DataFrame) -> LoaderValidationIssue:
    """Attach source-qualified identity to a strict-loader issue from raw adaptation."""
    raw_row = frame.iloc[issue.row_number]
    return LoaderValidationIssue(
        issue.row_number,
        issue.code,
        issue.disposition,
        issue.message,
        str(raw_row["raw_source_id"]),
        int(raw_row["raw_row_ordinal"]),
    )


def _finite_number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _validate_raw_book_ticks(
    row: Mapping[str, Any], contract: str, config: RawSnapshotAdapterConfig
) -> None:
    """Reject a raw book price that cannot be represented on its declared grid."""
    tick = float(config.tick_by_contract[contract])
    for level in range(5):
        for side in ("bid", "ask"):
            price = _finite_number(row[f"{side}px{level}"])
            if price is None or price <= 0:
                # The generic strict loader owns the canonical missing/invalid
                # book disposition and reports its row-level error.
                continue
            units = price / tick
            if not math.isclose(units, round(units), rel_tol=0.0, abs_tol=1e-9):
                raise FoundationContractError(
                    f"raw snapshot {side}px{level} is off the declared tick grid for {contract}"
                )


def validate_market_data(frame: pd.DataFrame, config: MarketDataValidationConfig) -> ValidatedMarketData:
    """Validate canonical book rows without silent cleaning or contract inference."""
    if not isinstance(frame, pd.DataFrame):
        raise FoundationContractError("market-data input must be a pandas DataFrame")
    if not isinstance(config, MarketDataValidationConfig):
        raise FoundationContractError("config must be a MarketDataValidationConfig")
    missing_columns = [column for column in config.required_columns if column not in frame.columns]
    if missing_columns:
        raise FoundationContractError(f"market-data input is missing required columns: {missing_columns}")
    if frame.empty:
        raise FoundationContractError("market-data input is empty")

    accepted: list[dict[str, Any]] = []
    issues: list[LoaderValidationIssue] = []
    previous_cumulative: dict[tuple[str, str], dict[str, float]] = {}

    for row_number, (_, raw_row) in enumerate(frame.iterrows()):
        row = raw_row.to_dict()
        contract = row["contract"]
        if not isinstance(contract, str) or not contract.strip() or contract not in config.declared_contract_universe:
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "undeclared_contract",
                "contract is missing or outside declared_contract_universe",
            )
            continue
        exchange_ts = _timestamp(row["exchange_ts"], config.source_timezone)
        recv_ts = _timestamp(row["recv_ts"], config.source_timezone)
        if exchange_ts is None or recv_ts is None:
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "invalid_timestamp",
                "exchange_ts and recv_ts must be valid timezone-aware timestamps",
            )
            continue
        if recv_ts < exchange_ts:
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "receive_before_exchange",
                "recv_ts must not precede exchange_ts",
            )
            continue
        source_seq = _integer(row["source_seq"])
        if source_seq is None or source_seq < 0:
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "invalid_source_sequence",
                "source_seq must be a non-negative integer",
            )
            continue
        book_error = _book_error(row, config)
        if book_error is not None:
            code, message, disposition = book_error
            _disposition(issues, disposition, row_number, code, message)
            continue
        cumulative_scope = _cumulative_scope(row, contract, config)
        if cumulative_scope is None:
            _disposition(
                issues,
                config.volume_correction_disposition,
                row_number,
                "invalid_cumulative_epoch",
                f"{config.cumulative_epoch_column} must be a non-empty string cumulative epoch",
            )
            continue
        cumulative_error = _cumulative_error(row, contract, cumulative_scope, previous_cumulative, config)
        if cumulative_error is not None:
            code, message = cumulative_error
            _disposition(issues, config.volume_correction_disposition, row_number, code, message)
            continue

        normalized = dict(row)
        normalized["exchange_ts"] = exchange_ts
        normalized["recv_ts"] = recv_ts
        normalized["source_seq"] = source_seq
        batch_id = (
            f"exchange-ts:{exchange_ts.isoformat()}"
            if config.exchange_batch_id_column is None
            else row.get(config.exchange_batch_id_column)
        )
        if not isinstance(batch_id, str) or not batch_id.strip():
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "invalid_exchange_batch_id",
                "exchange batch ID must be a non-empty string",
            )
            continue
        batch_seq = None if config.exchange_batch_seq_column is None else _integer(row.get(config.exchange_batch_seq_column))
        if config.exchange_batch_seq_column is not None and (batch_seq is None or batch_seq < 0):
            _disposition(
                issues,
                config.missing_data_disposition,
                row_number,
                "invalid_exchange_batch_sequence",
                "exchange batch sequence must be a non-negative integer",
            )
            continue
        normalized["exchange_batch_id"] = batch_id
        normalized["exchange_batch_seq"] = batch_seq
        for level in range(config.book_levels):
            normalized[f"bidpx{level}"] = float(normalized[f"bidpx{level}"])
            normalized[f"askpx{level}"] = float(normalized[f"askpx{level}"])
            normalized[f"bidvol{level}"] = int(normalized[f"bidvol{level}"])
            normalized[f"askvol{level}"] = int(normalized[f"askvol{level}"])
        for column in config.cumulative_volume_columns:
            normalized[column] = float(normalized[column])
        if config.cumulative_epoch_column is not None:
            normalized[config.cumulative_epoch_column] = cumulative_scope[1]
        if "passive_trades" in normalized:
            normalized["passive_trades"] = _normalize_passive_trades(normalized["passive_trades"])
        accepted.append(normalized)
        previous_cumulative.setdefault(cumulative_scope, {}).update(
            {column: float(normalized[column]) for column in config.cumulative_volume_columns}
        )

    _seal_exchange_batches(accepted, config)
    result_columns = tuple(dict.fromkeys((*frame.columns, "exchange_batch_id", "exchange_batch_seq")))
    result = pd.DataFrame(accepted, columns=result_columns)
    if result.empty:
        raise FoundationContractError("market-data validation accepted no rows")
    return ValidatedMarketData(result, config, tuple(issues))


def _seal_exchange_batches(accepted: list[dict[str, Any]], config: MarketDataValidationConfig) -> None:
    """Validate and deterministically order complete exchange snapshot batches.

    Receive/source ordering is deliberately excluded.  A timestamp is one
    batch unless the source declares a distinct batch ID and sequence.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        grouped.setdefault(str(row["exchange_batch_id"]), []).append(row)
    by_timestamp: dict[pd.Timestamp, list[tuple[str, list[dict[str, Any]]]]] = {}
    for batch_id, rows in grouped.items():
        timestamps = {pd.Timestamp(row["exchange_ts"]) for row in rows}
        if len(timestamps) != 1:
            raise FoundationContractError("one exchange batch ID cannot span more than one exchange timestamp")
        contracts = [str(row["contract"]) for row in rows]
        if len(contracts) != len(set(contracts)):
            raise FoundationContractError("an exchange batch cannot contain duplicate product snapshots")
        if config.require_complete_exchange_batches and set(contracts) != set(config.declared_contract_universe):
            raise FoundationContractError("an exchange batch must contain exactly the declared contract universe")
        sequences = {row["exchange_batch_seq"] for row in rows}
        if len(sequences) != 1:
            raise FoundationContractError("all rows in one exchange batch must share exchange_batch_seq")
        by_timestamp.setdefault(next(iter(timestamps)), []).append((batch_id, rows))

    batch_groups = [item for timestamp in by_timestamp.values() for item in timestamp]
    explicit = [rows[0]["exchange_batch_seq"] is not None for _, rows in batch_groups]
    if any(explicit) and not all(explicit):
        raise FoundationContractError("exchange batch sequencing must be explicit for every batch or for none")
    for timestamp, batches in by_timestamp.items():
        if len(batches) > 1:
            sequences = [rows[0]["exchange_batch_seq"] for _, rows in batches]
            if any(sequence is None for sequence in sequences) or len(set(sequences)) != len(sequences):
                raise FoundationContractError(
                    "distinct batches at one exchange timestamp require unique explicit exchange_batch_seq values"
                )

    if all(explicit):
        ordered_batches = sorted(
            batch_groups,
            key=lambda item: (pd.Timestamp(item[1][0]["exchange_ts"]), int(item[1][0]["exchange_batch_seq"]), item[0]),
        )
        sequences = [int(rows[0]["exchange_batch_seq"]) for _, rows in ordered_batches]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise FoundationContractError("exchange_batch_seq must define one strictly increasing global batch order")
    else:
        ordered_batches = sorted(batch_groups, key=lambda item: (pd.Timestamp(item[1][0]["exchange_ts"]), item[0]))
        for sequence, (_, rows) in enumerate(ordered_batches):
            for row in rows:
                row["exchange_batch_seq"] = sequence

    batch_order = {batch_id: index for index, (batch_id, _) in enumerate(ordered_batches)}
    accepted.sort(
        key=lambda row: (
            batch_order[str(row["exchange_batch_id"])],
            str(row["contract"]),
            str(row.get("raw_source_id", "")),
            int(row.get("raw_row_ordinal", 0)),
        )
    )


def _timestamp(value: object, source_timezone: str | None) -> pd.Timestamp | None:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        if source_timezone is None:
            return None
        timestamp = timestamp.tz_localize(ZoneInfo(source_timezone))
    return timestamp.tz_convert("UTC")


def _integer(value: object) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _normalize_passive_trades(value: object) -> list[dict[str, Any]]:
    """Validate optional co-received aggressor trades without inventing fills."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise FoundationContractError("passive_trades must be valid JSON when provided as a string") from exc
    if not isinstance(value, (list, tuple)):
        raise FoundationContractError("passive_trades must be a list of trade mappings")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_trade in enumerate(value):
        if not isinstance(raw_trade, dict):
            raise FoundationContractError("passive_trades entries must be mappings")
        try:
            taker_side = str(raw_trade["taker_side"])
            price = float(raw_trade["price"])
            quantity = _integer(raw_trade["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FoundationContractError("passive trade requires taker_side, price, and quantity") from exc
        if taker_side not in {"buy", "sell"} or not math.isfinite(price) or price <= 0 or quantity is None or quantity <= 0:
            raise FoundationContractError("passive trade fields are invalid")
        trade_id = raw_trade.get("trade_id")
        if trade_id is not None:
            if not isinstance(trade_id, str) or not trade_id.strip() or trade_id in ids:
                raise FoundationContractError("passive trade_id values must be unique non-empty strings per row")
            ids.add(trade_id)
        item = {"taker_side": taker_side, "price": price, "quantity": quantity}
        if trade_id is not None:
            item["trade_id"] = trade_id
        normalized.append(item)
    return normalized


def _book_error(
    row: dict[str, Any], config: MarketDataValidationConfig
) -> tuple[str, str, str] | None:
    bids: list[tuple[float, int]] = []
    asks: list[tuple[float, int]] = []
    for level in range(config.book_levels):
        bid_price, bid_volume = _finite_positive(row[f"bidpx{level}"]), _integer(row[f"bidvol{level}"])
        ask_price, ask_volume = _finite_positive(row[f"askpx{level}"]), _integer(row[f"askvol{level}"])
        if bid_price is None or ask_price is None or bid_volume is None or ask_volume is None:
            return "missing_book_field", "book prices must be finite positive values and volumes integers", config.missing_data_disposition
        if bid_volume < 0 or ask_volume < 0:
            return "negative_depth", "book depth must be non-negative", config.invalid_book_disposition
        bids.append((bid_price, bid_volume))
        asks.append((ask_price, ask_volume))
    if bids[0][1] == 0 or asks[0][1] == 0:
        return "zero_top_depth", "top-of-book depth is zero and must follow zero_depth_disposition", config.zero_depth_disposition
    active_bids = [price for price, volume in bids if volume > 0]
    active_asks = [price for price, volume in asks if volume > 0]
    if any(left <= right for left, right in zip(active_bids, active_bids[1:])):
        return "invalid_bid_order", "positive bid levels must be strictly descending", config.invalid_book_disposition
    if any(left >= right for left, right in zip(active_asks, active_asks[1:])):
        return "invalid_ask_order", "positive ask levels must be strictly ascending", config.invalid_book_disposition
    if active_bids[0] >= active_asks[0]:
        return "crossed_book", "best bid must be strictly below best ask", config.invalid_book_disposition
    return None


def _finite_positive(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _cumulative_scope(
    row: Mapping[str, Any], contract: str, config: MarketDataValidationConfig
) -> tuple[str, str] | None:
    """Return the source/reset-qualified monotonicity scope for a row."""
    if config.cumulative_epoch_column is None:
        return contract, ""
    epoch = row.get(config.cumulative_epoch_column)
    if not isinstance(epoch, str) or not epoch.strip():
        return None
    return contract, epoch


def _cumulative_error(
    row: dict[str, Any],
    contract: str,
    cumulative_scope: tuple[str, str],
    previous: dict[tuple[str, str], dict[str, float]],
    config: MarketDataValidationConfig,
) -> tuple[str, str] | None:
    prior = previous.get(cumulative_scope, {})
    for column in config.cumulative_volume_columns:
        try:
            numeric = float(row[column])
        except (TypeError, ValueError):
            return "invalid_cumulative_volume", f"{column} must be a finite non-negative cumulative value"
        if not math.isfinite(numeric) or numeric < 0:
            return "invalid_cumulative_volume", f"{column} must be a finite non-negative cumulative value"
        if column in prior and numeric < prior[column]:
            return "cumulative_volume_reset", f"{column} decreased for contract {contract}"
    return None


def _disposition(
    issues: list[LoaderValidationIssue], disposition: str, row_number: int, code: str, message: str
) -> None:
    issue = LoaderValidationIssue(row_number, code, disposition, message)
    issues.append(issue)
    if disposition == "reject":
        raise FoundationContractError(f"market-data validation rejected row {row_number}: {code}: {message}")


__all__ = [
    "LoaderValidationIssue",
    "MarketDataValidationConfig",
    "RawSnapshotAdapterConfig",
    "RawSnapshotAdapterProvenance",
    "RawSnapshotFile",
    "RawSnapshotFrame",
    "RawSnapshotSourceProvenance",
    "ValidatedMarketData",
    "adapt_raw_snapshot_frames",
    "adapted_replay_events_hash",
    "read_raw_snapshot_market_data",
    "read_validated_market_data",
    "validate_market_data",
]
