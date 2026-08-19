import math
import os

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from common.market import Market

from common.artifact_layout import (
    get_plot_dir,
    get_raw_dir,
    infer_session_artifact_parts,
    infer_underlying_tag,
)

from common.pnl_layers import (
    MESSAGE_FEE_COL,
    MESSAGE_FEE_DRAG_RATIO_COL,
    PNL_AFT_TRADE_FEE_COL,
    PNL_AFT_TRADE_FEE_MSG_COL,
    PNL_AFT_TRADE_FEE_PLUS_REBATE_COL,
    PNL_GROSS_COL,
    PNL_NET_COL,
    TRADE_FEE_COL,
    TRADE_FEE_DRAG_RATIO_COL,
    TRADE_FEE_REBATE_COL,
    build_pnl_layer_dict,
    enrich_strategy_pnl_frame,
)
from common.session_plotting import (
    add_event_strip,
    add_line_trace,
    add_marker_trace,
    add_state_heatmap,
    add_step_trace,
    create_interactive_figure,
    save_interactive_figure,
    should_generate_interactive_plot,
)

_CANCEL_REASON_COLORS = {
    "session_gate_open_delay": "#8c8c8c",
    "session_break": "#595959",
    "wind_down_reprice": "#fa8c16",
    "fair_move_bid": "#1890ff",
    "fair_move_ask": "#13c2c2",
    "max_resting_trim": "#722ed1",
    "filter_suppress": "#cf1322",
    "inventory_limit": "#eb2f96",
    "hedge_requote": "#2f54eb",
    "session_flatten": "#ad6800",
    "unknown": "#bfbfbf",
}


def _collapse_cancel_reason(reason_code: object) -> str:
    if not reason_code or pd.isna(reason_code):
        return "unknown"
    reason = str(reason_code)
    for prefix in (
        "session_gate_open_delay",
        "session_break",
        "wind_down_reprice",
        "fair_move",
        "max_resting_trim",
        "filter_suppress",
        "inventory_limit",
        "hedge_requote",
        "session_flatten",
    ):
        if reason.startswith(prefix):
            return prefix
    return reason


def _plot_state_bands(ax, record: pd.DataFrame, field_specs: list[tuple[str, str, object]], x_values=None) -> None:
    band_values = []
    band_labels = []
    for field_name, label, transform in field_specs:
        if field_name not in record.columns:
            continue
        series = record[field_name]
        values = (
            transform(series) if transform is not None else series.fillna(False).astype(bool).astype(float).to_numpy()
        )
        band_values.append(np.asarray(values, dtype=float))
        band_labels.append(label)

    if not band_values:
        ax.axis("off")
        return

    matrix = np.vstack(band_values)
    extent = None
    is_datetime_axis = False
    if x_values is not None:
        x_array = np.asarray(x_values)
        if np.issubdtype(x_array.dtype, np.datetime64):
            x_numeric = mdates.date2num(pd.to_datetime(x_values))
            is_datetime_axis = True
        else:
            x_numeric = x_array.astype(float)
        x_min = float(np.nanmin(x_numeric))
        x_max = float(np.nanmax(x_numeric))
        if x_min == x_max:
            x_max = x_min + (1.0 / 24.0 / 60.0 if is_datetime_axis else 1.0)
        extent = [x_min, x_max, -0.5, len(band_labels) - 0.5]

    ax.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="RdYlGn",
        vmin=0.0,
        vmax=1.0,
        extent=extent,
        origin="lower",
    )
    ax.set_yticks(np.arange(len(band_labels)))
    ax.set_yticklabels(band_labels)
    ax.set_xticks([])
    ax.set_title("Policy / state strips")
    ax.grid(False)
    if is_datetime_axis:
        ax.xaxis_date()


def _build_compressed_time_axis(
    x_values,
    *,
    gap_threshold: pd.Timedelta = pd.Timedelta(minutes=5),
    compressed_gap: pd.Timedelta = pd.Timedelta(minutes=1),
):
    timestamps = pd.Series(pd.to_datetime(x_values)).reset_index(drop=True)
    if timestamps.empty:
        return np.array([], dtype=float), timestamps, []

    display_x = np.zeros(len(timestamps), dtype=float)
    gap_markers = []
    compressed_gap_minutes = compressed_gap.total_seconds() / 60.0

    for idx in range(1, len(timestamps)):
        delta = timestamps.iloc[idx] - timestamps.iloc[idx - 1]
        if delta > gap_threshold:
            start_pos = display_x[idx - 1]
            display_x[idx] = start_pos + compressed_gap_minutes
            gap_markers.append(
                {
                    "start_pos": start_pos,
                    "end_pos": display_x[idx],
                    "center_pos": (start_pos + display_x[idx]) / 2.0,
                    "start_time": timestamps.iloc[idx - 1],
                    "end_time": timestamps.iloc[idx],
                }
            )
        else:
            display_x[idx] = display_x[idx - 1] + delta.total_seconds() / 60.0

    return display_x, timestamps, gap_markers


def _map_times_to_display_axis(x_lookup: pd.Series, event_times) -> np.ndarray:
    if x_lookup.empty:
        return np.array([], dtype=float)

    event_index = pd.to_datetime(event_times)
    mapped = x_lookup.reindex(event_index)
    return mapped.to_numpy(dtype=float)


def _shade_gap_regions(ax, gap_markers: list[dict[str, object]]) -> None:
    for gap in gap_markers:
        ax.axvspan(gap["start_pos"], gap["end_pos"], color="#f5f5f5", alpha=0.9, zorder=0)


def _apply_compressed_time_axis(
    ax,
    display_x: np.ndarray,
    timestamps: pd.Series,
    gap_markers: list[dict[str, object]],
    *,
    max_ticks: int = 8,
) -> None:
    if len(display_x) == 0:
        return

    _shade_gap_regions(ax, gap_markers)

    x_min = float(display_x[0])
    x_max = float(display_x[-1])
    if x_min == x_max:
        x_max = x_min + 1.0
    ax.set_xlim(x_min, x_max)

    tick_idx = np.unique(np.linspace(0, len(display_x) - 1, num=min(max_ticks, len(display_x)), dtype=int))
    ax.set_xticks(display_x[tick_idx])
    ax.set_xticklabels([timestamps.iloc[idx].strftime("%H:%M") for idx in tick_idx])

    for gap in gap_markers:
        ax.text(
            gap["center_pos"],
            -0.12,
            f"{gap['start_time'].strftime('%H:%M')}→{gap['end_time'].strftime('%H:%M')}",
            transform=ax.get_xaxis_transform(),
            ha="center",
            va="top",
            fontsize=8,
            color="#8c8c8c",
        )


def _mask_large_time_gaps(
    frame: pd.DataFrame,
    line_columns: list[str],
    *,
    gap_threshold: pd.Timedelta = pd.Timedelta(minutes=5),
) -> pd.DataFrame:
    if frame.empty or "datetime" not in frame.columns:
        return frame

    prepared = frame.copy()
    gap_mask = prepared["datetime"].diff() > gap_threshold
    columns = [column for column in line_columns if column in prepared.columns]
    if columns and gap_mask.any():
        prepared.loc[gap_mask, columns] = np.nan
    return prepared


def _plot_event_strip(
    ax, events: pd.DataFrame, event_specs: list[dict[str, object]], x_lookup: pd.Series | None = None
) -> None:
    if events.empty:
        ax.text(0.5, 0.5, "No event data", ha="center", va="center", transform=ax.transAxes)
        ax.set_title("Execution / cancel strip")
        ax.grid(False)
        return

    tick_positions: list[int] = []
    tick_labels: list[str] = []
    for idx, spec in enumerate(event_specs, start=1):
        mask = spec["mask"](events)
        subset = events.loc[mask]
        if subset.empty:
            continue
        tick_positions.append(idx)
        tick_labels.append(str(spec["label"]))
        x_values = (
            _map_times_to_display_axis(x_lookup, subset["event_time"]) if x_lookup is not None else subset["event_time"]
        )
        ax.scatter(
            x_values,
            np.full(len(subset), idx),
            color=spec["color"],
            marker=spec.get("marker", "o"),
            s=spec.get("size", 14),
            alpha=spec.get("alpha", 0.7),
            label=spec["label"],
        )

    if tick_positions:
        ax.set_yticks(tick_positions)
        ax.set_yticklabels(tick_labels)
    ax.grid(axis="x", alpha=0.2)
    ax.set_title("Execution / cancel strip")


def _prepare_strategy_record_frame(record: pd.DataFrame) -> pd.DataFrame:
    if record.empty:
        return record.copy()

    prepared = record.copy()
    if "datetime" in prepared.columns:
        prepared["datetime"] = pd.to_datetime(prepared["datetime"])
    return enrich_strategy_pnl_frame(prepared, drop_legacy_columns=True)


class Strategy:
    def __init__(
        self,
        name: str,
        market: Market,
        mult: int,
        tick: float,
        fee: float,
        rebate: float,
        fee_lot: float = None,
        parallel: bool = True,
        verbose: bool = False,
        output_root: str | None = None,
        record_diagnostics: bool = False,
        underlying: str | None = None,
    ):
        self.name = "<Strategy - " + name + ">"
        self.market = market
        self.mult = mult
        self.tick = tick
        self.fee = fee
        self.rebate = rebate
        self.fee_lot = fee_lot  # fixed $ per lot per side; overrides rate-based fee when set
        # self.stop_thresh = stop_thresh
        self.parallel = parallel
        self.verbose = verbose
        self.output_root = os.path.abspath(output_root) if output_root is not None else None
        self.record_diagnostics = record_diagnostics
        self.underlying = underlying

        # Calculate decimal places for price rounding based on tick size
        import numpy as np

        self.price_decimals = max(0, int(np.ceil(-np.log10(tick))))

        self.auto_unwind = False
        self.hedger = None
        self.curr_md = None
        self.trading_date = None

        self.fair_mid = None
        self.fair_ask = None
        self.fair_bid = None
        self.fair_value = None
        self.fair_upper = None
        self.fair_lower = None

        # self.reset()

    def snap_price(self, px: float):
        contract = getattr(self, "contract", None)
        if self.market is not None:
            if hasattr(self.market, "snap_price"):
                return self.market.snap_price(px, contract)
            if hasattr(self.market, "_snap_price"):
                return self.market._snap_price(px, contract)

        return self._normalize_price(px)

    def _normalize_price(self, px: float) -> float:
        contract = getattr(self, "contract", None)
        if self.market is not None and hasattr(self.market, "_normalize_price"):
            return self.market._normalize_price(px, contract)
        return round(round(px / self.tick) * self.tick, 12)

    def _resolve_book_level(self, book_side: dict[float, list[dict]], px: float) -> float | None:
        if self.market is not None and hasattr(self.market, "_resolve_book_level"):
            return self.market._resolve_book_level(book_side, px, getattr(self, "contract", None))

        normalized_px = self._normalize_price(px)
        price_tol = (
            self.market.price_tol_for(getattr(self, "contract", None))
            if self.market is not None and hasattr(self.market, "price_tol_for")
            else getattr(self.market, "price_tol", self.tick / 10)
        )
        if normalized_px in book_side:
            return normalized_px
        for level_px in book_side:
            if abs(level_px - normalized_px) <= price_tol:
                return level_px
        return None

    def _has_resting_level(self, book_side: dict[float, list[dict]], px: float) -> bool:
        return self._resolve_book_level(book_side, px) is not None

    def _resolve_output_directory(self, directory_name: str) -> str:
        if self.output_root is None:
            return directory_name
        return os.path.join(self.output_root, directory_name)

    def _infer_session_artifact_parts(self) -> tuple[str, str]:
        first_dt = None
        if self.session_record:
            first_dt = self.session_record[0].get("datetime")
        elif self.curr_md is not None:
            first_dt = self.curr_md.get("datetime")

        return infer_session_artifact_parts(first_dt)

    def _infer_underlying_tag(self) -> str:
        return infer_underlying_tag(getattr(self, "contract", None), fallback=self.underlying)

    def bind_instrument_spec(self, instrument_spec) -> None:
        """Bind one immutable product spec for a product-aware trading-day run."""
        if getattr(instrument_spec, "product", None) is None:
            raise ValueError("instrument_spec must declare a product")
        self.instrument_spec = instrument_spec
        self.session_calendar_id = instrument_spec.calendar.calendar_id
        self.mult = float(instrument_spec.multiplier)
        self.tick = float(instrument_spec.tick)
        self.price_decimals = max(0, int(np.ceil(-np.log10(self.tick))))

    def on_session_break(self, event) -> None:
        """Foundation lifecycle hook; deliberately preserves position and live state."""
        self.session_break_events.append(event)

    def on_eod(self, event):
        """Foundation lifecycle hook; Phase 1 represents EOD without synthetic fills."""
        outcome = {"status": "pending_execution_service", "event": event}
        self.eod_events.append(outcome)
        return outcome

    def reset(self, contract: str, trading_date: str | None = None):
        self.contract = contract
        self.trading_date = str(trading_date) if trading_date is not None else None
        self.pos = 0
        self.cost = None
        self.gross_pnl = 0
        self.pnl = 0
        self.total_fees = 0
        self.num_orders = 0
        self.num_fills = 0
        self.filled_qty = 0
        self.num_cancels = 0
        self.submitted_order_count = 0
        self.cancel_message_count = 0
        self.opened_qty = 0
        self.filled_order_ids = set()

        self.curr_md = None
        self.session_record = []
        self.event_log = []
        self.spread_pos = 0
        self.unhedged_exposure = 0
        self.quoting_enabled = True
        self.wind_down = False
        self.filter_suppress_bid = False
        self.filter_suppress_ask = False
        self.leg2_stale_warning = False
        self.leg2_tick_age_ms = np.nan
        self.pred_available = False
        self.pred_contract_1 = None
        self.pred_contract_2 = None
        self.pred_leg1_source_exchtime = pd.NaT
        self.pred_leg2_source_exchtime = pd.NaT
        self.pred_leg1_move = np.nan
        self.pred_leg2_move = np.nan
        self.pred_spread_move = np.nan
        self.pred_leg1_ticks = np.nan
        self.pred_leg2_ticks = np.nan
        self.pred_spread_ticks = np.nan
        self.pred_leg1_age_ms = np.nan
        self.pred_leg2_age_ms = np.nan
        self.pred_leg1_state = None
        self.pred_leg2_state = None
        self.pred_spread_state = None
        self.pred_sign_persistence_window = 0
        self.pred_leg1_sign = 0
        self.pred_leg2_sign = 0
        self.pred_spread_sign = 0
        self.pred_leg1_same_sign_count = 0
        self.pred_leg2_same_sign_count = 0
        self.pred_spread_same_sign_count = 0
        self.session_step_count = 0
        self.session_total_steps = None
        self.session_window = None
        self.session_break_events = []
        self.eod_events = []

    def get_pnl_metrics(self) -> dict[str, float]:
        gross_pnl = float(self.gross_pnl)
        pnl_after_fees = float(self.pnl)
        fee_drag = float(self.total_fees)
        rebate_pnl = fee_drag * float(self.rebate)
        net_pnl = pnl_after_fees + rebate_pnl
        fee_drag_ratio = fee_drag / gross_pnl if gross_pnl > 0 else np.nan
        return {
            "gross_pnl": gross_pnl,
            "pnl_after_fees": pnl_after_fees,
            "fee_drag": fee_drag,
            "fee_drag_ratio": fee_drag_ratio,
            "rebate_pnl": rebate_pnl,
            "net_pnl": net_pnl,
        }

    def record_event(
        self,
        event_type: str,
        price: float | None,
        qty: int | float,
        *,
        order: dict | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        extra: dict | None = None,
        event_time_override=None,
    ) -> None:
        if not self.record_diagnostics or self.curr_md is None:
            return

        event_time = pd.Timestamp(event_time_override) if event_time_override is not None else self.curr_md["datetime"]
        created_at = order.get("created_at") if order is not None else None
        age_ms = None
        if created_at is not None:
            age_ms = (pd.Timestamp(event_time) - pd.Timestamp(created_at)).total_seconds() * 1000.0

        resolved_reason = (
            reason_code if reason_code is not None else (order.get("reason_code") if order is not None else None)
        )
        event = {
            "time": event_time,
            "event": event_type,
            "event_time": event_time,
            "event_type": event_type,
            "strategy_name": self.name,
            "contract": getattr(self, "contract", None),
            "product": getattr(self, "contract", None),
            "calendar_id": getattr(self, "session_calendar_id", None),
            "trading_day": self.trading_date,
            "session_window": getattr(self, "session_window", None),
            "price": price,
            "qty": qty,
            "abs_qty": abs(qty) if qty is not None else np.nan,
            "side": "buy" if qty >= 0 else "sell",
            "order_id": order.get("order_id") if order is not None else None,
            "created_at": created_at,
            "created_tick": order.get("created_tick") if order is not None else None,
            "age_ticks": order.get("age_ticks", order.get("count") if order is not None else None)
            if order is not None
            else None,
            "age_ms": age_ms,
            "aggressive": order.get("aggressive") if order is not None else None,
            "queue_before": order.get("queue_before", order.get("queue") if order is not None else None)
            if order is not None
            else None,
            "queue_after": order.get("queue_after") if order is not None else None,
            "reason_code": resolved_reason,
            "reason_detail": reason_detail
            if reason_detail is not None
            else (order.get("reason_detail") if order is not None else None),
            "trading_date": getattr(self, "trading_date", None),
            "md_bid": self.curr_md.get("bidpx0"),
            "md_ask": self.curr_md.get("askpx0"),
            "md_traded": self.curr_md.get("traded"),
            "fair_mid": self.fair_mid,
            "fair_bid": self.fair_bid,
            "fair_ask": self.fair_ask,
            "strategy_pos": self.pos,
            "spread_pos": getattr(self, "spread_pos", np.nan),
            "unhedged_exposure": getattr(self, "unhedged_exposure", np.nan),
            "quoting_enabled": getattr(self, "quoting_enabled", np.nan),
            "wind_down": getattr(self, "wind_down", np.nan),
            "filter_suppress_bid": getattr(self, "filter_suppress_bid", np.nan),
            "filter_suppress_ask": getattr(self, "filter_suppress_ask", np.nan),
            "leg2_stale_warning": getattr(self, "leg2_stale_warning", np.nan),
            "leg2_tick_age_ms": getattr(self, "leg2_tick_age_ms", np.nan),
            "prediction_policy_enabled": getattr(self, "prediction_policy_enabled", False),
            "prediction_diagnostics_only": getattr(self, "prediction_diagnostics_only", False),
            "pred_available": getattr(self, "pred_available", False),
            "pred_contract_1": getattr(self, "pred_contract_1", None),
            "pred_contract_2": getattr(self, "pred_contract_2", None),
            "pred_leg1_source_exchtime": getattr(self, "pred_leg1_source_exchtime", pd.NaT),
            "pred_leg2_source_exchtime": getattr(self, "pred_leg2_source_exchtime", pd.NaT),
            "pred_leg1_move": getattr(self, "pred_leg1_move", np.nan),
            "pred_leg2_move": getattr(self, "pred_leg2_move", np.nan),
            "pred_spread_move": getattr(self, "pred_spread_move", np.nan),
            "pred_leg1_ticks": getattr(self, "pred_leg1_ticks", np.nan),
            "pred_leg2_ticks": getattr(self, "pred_leg2_ticks", np.nan),
            "pred_spread_ticks": getattr(self, "pred_spread_ticks", np.nan),
            "pred_leg1_age_ms": getattr(self, "pred_leg1_age_ms", np.nan),
            "pred_leg2_age_ms": getattr(self, "pred_leg2_age_ms", np.nan),
            "pred_leg1_state": getattr(self, "pred_leg1_state", None),
            "pred_leg2_state": getattr(self, "pred_leg2_state", None),
            "pred_spread_state": getattr(self, "pred_spread_state", None),
            "pred_sign_persistence_window": getattr(self, "pred_sign_persistence_window", 0),
            "pred_leg1_sign": getattr(self, "pred_leg1_sign", 0),
            "pred_leg2_sign": getattr(self, "pred_leg2_sign", 0),
            "pred_spread_sign": getattr(self, "pred_spread_sign", 0),
            "pred_leg1_same_sign_count": getattr(self, "pred_leg1_same_sign_count", 0),
            "pred_leg2_same_sign_count": getattr(self, "pred_leg2_same_sign_count", 0),
            "pred_spread_same_sign_count": getattr(self, "pred_spread_same_sign_count", 0),
            "hedge_episode_id": order.get("hedge_episode_id") if order is not None else None,
            "hedge_action": order.get("hedge_action") if order is not None else None,
            "effective_offset": order.get("effective_offset") if order is not None else None,
            "requote_count": order.get("requote_count") if order is not None else None,
        }
        if order is not None:
            for key in (
                "quote_action",
                "close_type_candidate",
                "close_urgency",
                "submit_aggressive",
                "submit_time",
                "submit_mid_spread",
                "submit_fair_spread",
                "submit_mid_dev",
                "submit_distance_ticks",
                "submit_in_active_zone",
                "submit_is_far_order",
                "submit_expected_hedge_px",
                "submit_expected_locked_spread",
                "submit_expected_edge_ticks",
                "expected_close_pnl_ticks",
                "primary_holding_ms",
                "hedge_trigger_time",
                "hedge_pricing_md_time",
                "hedge_pricing_bid0",
                "hedge_pricing_ask0",
                "post_fill_route",
                "post_fill_trigger_id",
                "post_fill_route_reason",
                "post_fill_route_applied",
                "post_fill_leg1_favorable_ticks",
                "post_fill_leg1_adverse_ticks",
                "post_fill_leg2_adverse_ticks",
                "base_close_candidate",
                "prediction_close_candidate",
                "prediction_base_allow_close",
                "prediction_changed_allow_close",
                "prediction_additional_roundtrip_candidate",
                "prediction_close_trigger_id",
                "prediction_close_urgency",
                "prediction_close_mode",
                "prediction_hedge_extra_offset_ticks",
                "prediction_hedge_reason",
                "prediction_close_override",
                "prediction_close_reason",
                "hedge_route_mode",
                "hedge_route_decision",
                "hedge_route_reason",
                "hedge_price_mode",
                "hedge_allow_escalation",
                "hedge_aggressive",
                "hedge_passive_join_top",
                "fallback_hedge_mode",
                "fallback_hedge_aggressive",
                # Snapshot-interval execution audit fields.  These are
                # intentionally generic so pair policies can retain the raw
                # pricing/execution boundary and level-by-level VWAP evidence
                # in their normal fill diagnostics.
                "interval_limit",
                "interval_limit_px",
                "interval_order",
                "placement_snapshot_time",
                "pricing_snapshot_time",
                "execution_snapshot_time",
                "execution_levels",
                "taker_phase",
            ):
                if key in order:
                    event[key] = order.get(key)
        if extra:
            event.update(extra)
        self.event_log.append(event)

    def submit_order(
        self,
        px: float,
        qty: int,
        *,
        aggressive: bool = False,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        extra_metadata: dict | None = None,
        event_time_override=None,
    ) -> dict | None:
        metadata = {"strategy_name": self.name}
        if reason_code is not None:
            metadata["reason_code"] = reason_code
        if reason_detail is not None:
            metadata["reason_detail"] = reason_detail
        if extra_metadata:
            metadata.update(extra_metadata)

        normalized_px = self._normalize_price(px)

        order = self.market.place_order(
            self.contract,
            normalized_px,
            qty,
            aggressive=aggressive,
            metadata=metadata,
            event_time=event_time_override,
        )
        self.num_orders += 1
        self.submitted_order_count += 1
        self.record_event(
            "order",
            normalized_px,
            qty,
            order=order,
            reason_code=reason_code,
            reason_detail=reason_detail,
            extra=extra_metadata,
            event_time_override=event_time_override,
        )
        return order

    def set_fair(self, fair_mid: float, fair_ask: float, fair_bid: float):
        self.fair_mid = fair_mid
        self.fair_ask = fair_ask
        self.fair_bid = fair_bid
        self.fair_value = fair_mid
        self.fair_upper = fair_ask
        self.fair_lower = fair_bid

        if self.verbose:
            print(
                f" - Fair mid set to {self.fair_mid}, ask: {self.fair_ask}, bid: {self.fair_bid}"
                f" for strategy {self.name} on contract {self.contract}"
            )

    def set_hedger(self, hedger: "Strategy"):
        self.hedger = hedger

    def _msg_limit_reached(self, contract=None):
        """True when the contract has hit its per-trading-day order-message limit.

        Pure predicate (no side effects): used by entry gates to SUPPRESS NEW
        ENTRIES only — exits / hedges / cancels are never gated by this. Safe
        no-op when no tracker is attached (feature off / unit tests) or before
        the first MD tick.
        """
        tracker = getattr(self.market, "order_limit", None)
        if tracker is None or self.curr_md is None:
            return False
        contract = contract if contract is not None else self.contract
        spec = getattr(self, "instrument_spec", None)
        return tracker.over_limit(contract, self.curr_md["datetime"], calendar=getattr(spec, "calendar", None))

    def step(self, md):
        if md["contract"] != self.contract:
            return

        self.curr_md = md
        self.session_step_count += 1

        if self.verbose:
            print(f"\n - Strategy {self.name} stepping on MD:")
            print(md)
            print(" - Asks:", self.market.asks)
            print(" - Bids:", self.market.bids)
            print(" - Pos:", self.pos)
            print(" - Cost:", self.cost)
            print(" - PnL:", round(self.pnl / 1000, 1), "K")
            print(" - Fees:", round(self.total_fees / 1000, 1), "K")

        filled = self.market.match(self.contract)

        if len(filled) > 0:
            self.num_fills += len(filled)
            self.filled_qty += sum(abs(order["qty"]) for order in filled)
            self.update_pos(filled)

            if self.auto_unwind:
                for order in filled:
                    self.unwind(order, 1)

        if self.verbose:
            print("\n - Updated Asks:", self.market.asks)
            print(" - Updated Bids:", self.market.bids)
            print(" - Updated Pos:", self.pos)
            print(" - Updated Cost:", self.cost)
            print(" - Updated PnL:", round(self.pnl / 1000, 1), "K")
            print(" - Updated Fees:", round(self.total_fees / 1000, 1), "K")

        # if not self.stop_loss():
        self.quote(md)

        if self.record_diagnostics or not self.parallel:
            pnl_metrics = self.get_pnl_metrics()
            pnl_layers = build_pnl_layer_dict(
                pnl_gross=float(pnl_metrics["gross_pnl"]),
                pnl_aft_trade_fee=float(self.pnl),
                trade_fee=float(self.total_fees),
                trade_fee_rebate=float(pnl_metrics["rebate_pnl"]),
                message_fee=0.0,
            )
            record = {
                "datetime": md["datetime"],
                "product": self.contract,
                "calendar_id": getattr(self, "session_calendar_id", None),
                "trading_day": self.trading_date,
                "session_window": getattr(self, "session_window", None),
                "pos": self.pos,
                "bid": md["bidpx0"],
                "ask": md["askpx0"],
                "traded": md["traded"],
                "fair_mid": self.fair_mid,
                "fair_ask": self.fair_ask,
                "fair_bid": self.fair_bid,
                "fair_upper": self.fair_upper,
                "fair_lower": self.fair_lower,
                "cost": self.cost,
                "pnl": self.pnl,
                "fees": self.total_fees,
                PNL_GROSS_COL: pnl_layers[PNL_GROSS_COL],
                PNL_AFT_TRADE_FEE_COL: pnl_layers[PNL_AFT_TRADE_FEE_COL],
                PNL_AFT_TRADE_FEE_MSG_COL: pnl_layers[PNL_AFT_TRADE_FEE_MSG_COL],
                PNL_AFT_TRADE_FEE_PLUS_REBATE_COL: pnl_layers[PNL_AFT_TRADE_FEE_PLUS_REBATE_COL],
                PNL_NET_COL: pnl_layers[PNL_NET_COL],
                TRADE_FEE_COL: pnl_layers[TRADE_FEE_COL],
                TRADE_FEE_REBATE_COL: pnl_layers[TRADE_FEE_REBATE_COL],
                MESSAGE_FEE_COL: pnl_layers[MESSAGE_FEE_COL],
                TRADE_FEE_DRAG_RATIO_COL: pnl_layers[TRADE_FEE_DRAG_RATIO_COL],
                MESSAGE_FEE_DRAG_RATIO_COL: pnl_layers[MESSAGE_FEE_DRAG_RATIO_COL],
            }
            extra = self.get_extra()
            record.update(extra)
            self.session_record.append(record)

        if self.verbose:
            print(f" - Strategy {self.name} step completed.\n")
            input("Press Enter to continue...")

    def update_pos(self, filled):
        gross_pnl = 0
        pnl_after_fees = 0
        fees = 0
        opened_qty = 0

        for order in filled:
            px = order["px"]
            qty = order["qty"]
            order_id = order.get("order_id")

            if order_id is not None:
                self.filled_order_ids.add(order_id)

            self.record_event("fill", px, qty, order=order)

            if qty > 0:
                if self.pos > 0:
                    opened_qty += qty
                    v = self.pos * self.cost + px * qty
                    self.pos += qty
                    self.cost = v / self.pos
                elif self.pos == 0:
                    opened_qty += qty
                    self.pos = qty
                    self.cost = px
                elif self.pos < 0:
                    close = min(-self.pos, qty)
                    self.pos += close
                    gross_close = (self.cost - px) * close * self.mult
                    if self.fee_lot is not None:
                        fees += close * self.fee_lot
                        pnl_after_fees += gross_close - close * self.fee_lot
                    else:
                        fees += close * px * self.mult * self.fee
                        pnl_after_fees += gross_close - close * px * self.mult * self.fee
                    gross_pnl += gross_close
                    qty -= close

                    if qty > 0:
                        opened_qty += qty
                        self.pos = qty
                        self.cost = px
            elif qty < 0:
                if self.pos < 0:
                    opened_qty += -qty
                    v = self.pos * self.cost + px * qty
                    self.pos += qty
                    self.cost = v / self.pos
                elif self.pos == 0:
                    opened_qty += -qty
                    self.pos = qty
                    self.cost = px
                elif self.pos > 0:
                    close = min(self.pos, -qty)
                    self.pos -= close
                    gross_close = (px - self.cost) * close * self.mult
                    if self.fee_lot is not None:
                        fees += close * self.fee_lot
                        pnl_after_fees += gross_close - close * self.fee_lot
                    else:
                        fees += close * px * self.mult * self.fee
                        pnl_after_fees += gross_close - close * px * self.mult * self.fee
                    gross_pnl += gross_close
                    qty += close

                    if qty < 0:
                        opened_qty += -qty
                        self.pos = qty
                        self.cost = px

        self.gross_pnl += gross_pnl
        self.pnl += pnl_after_fees
        self.total_fees += fees
        self.opened_qty += opened_qty

        return pnl_after_fees

    def quote(self, md):
        pass

    def bid_and_cancel(
        self, px_list, min_count=0, cancel_reason_code: str | None = None, order_reason_code: str | None = None
    ):
        # Pre-compute rounded prices for fast lookup
        px_list_rounded_set = {self._normalize_price(px) for px in px_list}
        existing_bids = self.market.bids.get(self.contract, {})

        # Find prices to cancel using set lookup (O(1) instead of O(n))
        to_cancel = [px for px in existing_bids.keys() if self._normalize_price(px) not in px_list_rounded_set]

        # # Debug: log what we're trying to cancel
        # if self.verbose or (len(existing_bids) > 0 and len(to_cancel) == 0 and len(px_list) > 0):
        #     for px, orders in existing_bids.items():
        #         rounded_px = round(px, self.price_decimals)
        #         for order in orders:
        #             print(f"  [DEBUG] Existing bid @ {px} (rounded: {rounded_px}): qty={order['qty']}, count={order['count']}, min_count={min_count}, in_px_list={rounded_px in px_list_rounded_set}")

        total = 0
        for px in to_cancel:
            if self.verbose:
                print(f" - Cancelling bid @ {px} for strategy {self.name}")

            for order in existing_bids.get(px, []):
                self.log_cancel(px, order["qty"], order=order, reason_code=cancel_reason_code)

            cancelled_qty = self.cancel_bid_level(px, min_count=min_count)
            total += cancelled_qty

        level_qty = math.ceil(total / len(px_list)) if len(px_list) > 0 else 0

        for px in px_list:
            # if px not in self.market.bids.get(self.contract, {}):
            if total > 0:
                order_qty = min(level_qty, total)
                total -= order_qty

                if order_qty > 0:
                    if self.verbose:
                        print(f" - Placing bid @ {px} for strategy {self.name}")

                    # Round price to tick size
                    rounded_px = self._normalize_price(px)
                    self.submit_order(rounded_px, order_qty, reason_code=order_reason_code)

    def ask_and_cancel(
        self, px_list, min_count=0, cancel_reason_code: str | None = None, order_reason_code: str | None = None
    ):
        # Pre-compute rounded prices for fast lookup
        px_list_rounded_set = {self._normalize_price(px) for px in px_list}
        existing_asks = self.market.asks.get(self.contract, {})

        # Find prices to cancel using set lookup (O(1) instead of O(n))
        to_cancel = [px for px in existing_asks.keys() if self._normalize_price(px) not in px_list_rounded_set]

        # # Debug: log what we're trying to cancel
        # if self.verbose or (len(existing_asks) > 0 and len(to_cancel) == 0 and len(px_list) > 0):
        #     for px, orders in existing_asks.items():
        #         rounded_px = round(px, self.price_decimals)
        #         for order in orders:
        #             print(f"  [DEBUG] Existing ask @ {px} (rounded: {rounded_px}): qty={order['qty']}, count={order['count']}, min_count={min_count}, in_px_list={rounded_px in px_list_rounded_set}")

        total = 0
        for px in to_cancel:
            if self.verbose:
                print(f" - Cancelling ask @ {px} for strategy {self.name}")

            for order in existing_asks.get(px, []):
                self.log_cancel(px, order["qty"], order=order, reason_code=cancel_reason_code)

            cancelled_qty = self.cancel_ask_level(px, min_count=min_count)
            total += cancelled_qty

        level_qty = math.floor(total / len(px_list)) if len(px_list) > 0 else 0

        for px in px_list:
            # if px not in self.market.asks.get(self.contract, {}):
            if total < 0:
                order_qty = max(level_qty, total)
                total -= order_qty

                if order_qty < 0:
                    if self.verbose:
                        print(f" - Placing ask @ {px} for strategy {self.name}")

                    # Round price to tick size
                    rounded_px = self._normalize_price(px)
                    self.submit_order(rounded_px, order_qty, reason_code=order_reason_code)

    def unwind(self, trade, offset=1):
        px = trade["px"]
        qty = trade["qty"]

        if qty < 0:  # and self.pos < 0:
            target = self._normalize_price(px - offset * self.tick)

            if not self._has_resting_level(self.market.bids.get(self.contract, {}), target):
                if self.verbose:
                    print(f" - Placing bid to unwind @ {target}")

                self.submit_order(target, -qty, reason_code="auto_unwind")
        elif qty > 0:  # and self.pos > 0:
            target = self._normalize_price(px + offset * self.tick)

            if not self._has_resting_level(self.market.asks.get(self.contract, {}), target):
                if self.verbose:
                    print(f" - Placing ask to unwind @ {target}")

                self.submit_order(target, -qty, reason_code="auto_unwind")

    def stop_loss(self):
        if self.pos > 0:
            mtm = self.curr_md["bidpx0"] - self.cost
        elif self.pos < 0:
            mtm = self.cost - self.curr_md["askpx0"]
        else:
            mtm = 0

        # if mtm < -self.stop_thresh:
        #     if self.verbose:
        #         print(f"\n - Stop loss triggered for strategy {self.name}. Current MTM: {mtm:.2f}, Stop Loss: {-self.stop_thresh:.2f}")

        #     self.stop()

        #     return True

        return False

    def log_cancel(
        self,
        px: float,
        qty: int,
        *,
        order: dict | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
    ):
        """Log a cancel event to event_log (non-parallel mode only)."""
        self.record_event("cancel", px, qty, order=order, reason_code=reason_code, reason_detail=reason_detail)

    def log_cancel_book_side(self, book_side: dict, reason_code: str | None = None, reason_detail: str | None = None):
        """Log all resting orders in a book side (bids or asks) as cancel events."""
        for px, orders in book_side.items():
            for order in orders:
                self.log_cancel(px, order["qty"], order=order, reason_code=reason_code, reason_detail=reason_detail)

    def _count_cancellable_orders(self, book_side: dict, px: float, min_count: int = 0) -> int:
        level_px = self._resolve_book_level(book_side, px)
        if level_px is None:
            return 0
        return sum(1 for order in book_side[level_px] if order["count"] >= min_count)

    def _count_resting_orders(self, book_side: dict) -> int:
        return sum(len(orders) for orders in book_side.values())

    def cancel_bid_level(self, px: float, min_count: int = 0, *, include_cancel_stat: bool = True) -> int:
        bid_book = self.market.bids.get(self.contract, {})
        level_px = self._resolve_book_level(bid_book, px)
        if level_px is None:
            return 0
        cancelled_order_count = self._count_cancellable_orders(bid_book, level_px, min_count=min_count)
        cancelled_qty = self.market.cancel_bids(self.contract, level_px, min_count)
        if cancelled_qty != 0:
            if include_cancel_stat:
                self.num_cancels += 1
            self.cancel_message_count += cancelled_order_count
        return cancelled_qty

    def cancel_ask_level(self, px: float, min_count: int = 0, *, include_cancel_stat: bool = True) -> int:
        ask_book = self.market.asks.get(self.contract, {})
        level_px = self._resolve_book_level(ask_book, px)
        if level_px is None:
            return 0
        cancelled_order_count = self._count_cancellable_orders(ask_book, level_px, min_count=min_count)
        cancelled_qty = self.market.cancel_asks(self.contract, level_px, min_count)
        if cancelled_qty != 0:
            if include_cancel_stat:
                self.num_cancels += 1
            self.cancel_message_count += cancelled_order_count
        return cancelled_qty

    def cancel_all_resting_bids(self, *, include_cancel_stat: bool = True) -> int:
        bid_book = self.market.bids.get(self.contract, {})
        resting_order_count = self._count_resting_orders(bid_book)
        cancelled_levels = self.market.cancel_all_bids(self.contract)
        if cancelled_levels:
            if include_cancel_stat:
                self.num_cancels += cancelled_levels
            self.cancel_message_count += resting_order_count
        return cancelled_levels

    def cancel_all_resting_asks(self, *, include_cancel_stat: bool = True) -> int:
        ask_book = self.market.asks.get(self.contract, {})
        resting_order_count = self._count_resting_orders(ask_book)
        cancelled_levels = self.market.cancel_all_asks(self.contract)
        if cancelled_levels:
            if include_cancel_stat:
                self.num_cancels += cancelled_levels
            self.cancel_message_count += resting_order_count
        return cancelled_levels

    def stop(self):
        if self.verbose:
            print(f"\n - Stopping strategy {self.name}...")

        bid_book = self.market.bids.get(self.contract, {})
        ask_book = self.market.asks.get(self.contract, {})
        self.log_cancel_book_side(bid_book, reason_code="session_flatten")
        self.log_cancel_book_side(ask_book, reason_code="session_flatten")
        self.cancel_all_resting_bids(include_cancel_stat=False)
        self.cancel_all_resting_asks(include_cancel_stat=False)

        filled = None

        if self.pos > 0:
            filled = {"px": self.curr_md["bidpx0"], "qty": -self.pos}
        elif self.pos < 0:
            filled = {"px": self.curr_md["askpx0"], "qty": -self.pos}

        if filled is not None:
            self.update_pos([filled])
            self.num_orders += 1
            self.num_fills += 1
            self.filled_qty += abs(filled["qty"])

    def hedge(
        self,
        size,
        offset,
        reason_code: str | None = None,
        event_metadata: dict | None = None,
        event_time_override=None,
    ):
        if self.curr_md is None:
            return

        if size > 0:
            px = self.curr_md["askpx0"] + offset * self.tick
            self.submit_order(
                px,
                size,
                aggressive=True,
                reason_code=reason_code or "hedge_submit",
                extra_metadata=event_metadata,
                event_time_override=event_time_override,
            )
        elif size < 0:
            px = self.curr_md["bidpx0"] - offset * self.tick
            self.submit_order(
                px,
                size,
                aggressive=True,
                reason_code=reason_code or "hedge_submit",
                extra_metadata=event_metadata,
                event_time_override=event_time_override,
            )
        else:
            return

    def summarize(self):
        pnl_metrics = self.get_pnl_metrics()
        return {
            "gross_pnl": pnl_metrics["gross_pnl"],
            "pnl": self.pnl,
            "fees": self.total_fees,
            "fee_drag": pnl_metrics["fee_drag"],
            "fee_drag_ratio": pnl_metrics["fee_drag_ratio"],
            "rebate_pnl": pnl_metrics["rebate_pnl"],
            "net_pnl": pnl_metrics["net_pnl"],
            "num_orders": self.num_orders,
            "num_fills": self.num_fills,
            "filled_qty": self.filled_qty,
            "num_cancels": self.num_cancels,
            "submitted_order_count": self.submitted_order_count,
            "cancel_message_count": self.cancel_message_count,
            "traded_order_count": len(self.filled_order_ids),
            "opened_qty": self.opened_qty,
        }

    def plot_record(self):
        if not self.session_record:
            print(f"No session record to plot for {self.contract}.")
            return

        record = _prepare_strategy_record_frame(pd.DataFrame(self.session_record))
        plot_record = _mask_large_time_gaps(
            record,
            [
                "traded",
                "bid",
                "ask",
                "fair_mid",
                "fair_ask",
                "fair_bid",
                "target_bid_px",
                "target_ask_px",
                "best_resting_bid_px",
                "best_resting_ask_px",
                "cost",
                PNL_GROSS_COL,
                PNL_AFT_TRADE_FEE_COL,
                PNL_AFT_TRADE_FEE_MSG_COL,
                PNL_AFT_TRADE_FEE_PLUS_REBATE_COL,
                PNL_NET_COL,
                TRADE_FEE_COL,
                TRADE_FEE_REBATE_COL,
                MESSAGE_FEE_COL,
                "pos",
                "spread_pos",
                "unhedged_exposure",
                "hedge_need_qty",
            ],
        )
        events = pd.DataFrame(self.event_log) if self.event_log else pd.DataFrame()
        if not events.empty:
            events["event_time"] = pd.to_datetime(events["event_time"])
            events["reason_group"] = events["reason_code"].map(_collapse_cancel_reason)

        fig, axes = plt.subplots(
            5, 1, sharex=True, figsize=(18, 13.5), gridspec_kw={"height_ratios": [5, 2, 2, 1.25, 1.5]}
        )
        ax1, ax2, ax3, ax4, ax5 = axes
        display_x, display_time, gap_markers = _build_compressed_time_axis(plot_record["datetime"])
        x_lookup = pd.Series(display_x, index=display_time).groupby(level=0).last()
        x = display_x

        if {"bid", "ask"}.issubset(plot_record.columns):
            ax1.fill_between(
                x, plot_record["bid"], plot_record["ask"], color="#d9d9d9", alpha=0.18, label="Market Spread"
            )
        ax1.plot(x, plot_record["traded"], color="#262626", linewidth=1, alpha=0.7, label="Traded")
        if "fair_mid" in plot_record.columns:
            ax1.plot(
                x,
                plot_record["fair_mid"],
                color="#fa8c16",
                alpha=0.85,
                linewidth=1.05,
                linestyle="--",
                label="Fair Mid",
            )
        ax1.plot(x, plot_record["fair_ask"], color="#faad14", alpha=0.45, linewidth=0.9, label="Fair Ask")
        ax1.plot(x, plot_record["fair_bid"], color="#d48806", alpha=0.45, linewidth=0.9, label="Fair Bid")
        if "target_bid_px" in plot_record.columns:
            ax1.plot(x, plot_record["target_bid_px"], color="#0958d9", linewidth=1.0, linestyle=":", label="Target Bid")
        if "target_ask_px" in plot_record.columns:
            ax1.plot(x, plot_record["target_ask_px"], color="#08979c", linewidth=1.0, linestyle=":", label="Target Ask")
        ax1.plot(x, plot_record["cost"], color="#ff85c0", linewidth=1.8, alpha=0.9, label="Cost")

        if not events.empty:
            fills = events.loc[events["event_type"] == "fill"]
            if not fills.empty:
                buy_fills = fills.loc[fills["side"] == "buy"]
                sell_fills = fills.loc[fills["side"] == "sell"]
                if not buy_fills.empty:
                    ax1.scatter(
                        _map_times_to_display_axis(x_lookup, buy_fills["event_time"]),
                        buy_fills["price"],
                        color="#52c41a",
                        marker="^",
                        s=24,
                        alpha=0.9,
                        label="Fill Buy",
                    )
                if not sell_fills.empty:
                    ax1.scatter(
                        _map_times_to_display_axis(x_lookup, sell_fills["event_time"]),
                        sell_fills["price"],
                        color="#f5222d",
                        marker="v",
                        s=24,
                        alpha=0.9,
                        label="Fill Sell",
                    )

        handles, labels = ax1.get_legend_handles_labels()
        dedup: dict[str, object] = {}
        for handle, label in zip(handles, labels):
            dedup.setdefault(label, handle)
        ax1.legend(dedup.values(), dedup.keys(), loc="upper left", ncol=3, fontsize=8)
        ax1.grid()
        ax1.set_title(self.contract)

        ax2.step(x, plot_record[PNL_GROSS_COL], where="post", color="#52c41a", linewidth=1.15, label=PNL_GROSS_COL)
        ax2.step(
            x,
            plot_record[PNL_AFT_TRADE_FEE_COL],
            where="post",
            color="#0958d9",
            linewidth=1.1,
            label=PNL_AFT_TRADE_FEE_COL,
        )
        ax2.step(
            x,
            plot_record[PNL_AFT_TRADE_FEE_MSG_COL],
            where="post",
            color="#531dab",
            linewidth=1.1,
            label=PNL_AFT_TRADE_FEE_MSG_COL,
        )
        ax2.step(
            x,
            plot_record[PNL_AFT_TRADE_FEE_PLUS_REBATE_COL],
            where="post",
            color="#d46b08",
            linewidth=1.1,
            label=PNL_AFT_TRADE_FEE_PLUS_REBATE_COL,
        )
        ax2.step(x, plot_record[PNL_NET_COL], where="post", color="#cf1322", linewidth=1.35, label=PNL_NET_COL)
        ax2.legend(loc="upper left", ncol=2, fontsize=8)
        ax2.grid()
        ax2.set_title("Strategy PnL layers (exact PNL_* definitions)")

        ax3.step(x, plot_record["pos"], where="post", color="#cf1322", linewidth=1.1, label="Leg Pos")
        if "spread_pos" in plot_record.columns:
            ax3.step(x, plot_record["spread_pos"], where="post", color="#2f54eb", linewidth=1.0, label="Spread Pos")
        if "unhedged_exposure" in plot_record.columns:
            ax3.step(
                x,
                plot_record["unhedged_exposure"],
                where="post",
                color="#fa8c16",
                linewidth=1.0,
                label="Unhedged Exposure",
            )
        if "hedge_need_qty" in plot_record.columns:
            ax3.step(x, plot_record["hedge_need_qty"], where="post", color="#722ed1", linewidth=1.0, label="Hedge Need")
        ax3.legend(loc="upper left", ncol=4, fontsize=8)
        ax3.grid()
        ax3.set_title("Position / exposure")

        _plot_event_strip(
            ax4,
            events,
            [
                {
                    "label": "Fill Buy",
                    "mask": lambda frame: (frame["event_type"] == "fill") & (frame["side"] == "buy"),
                    "color": "#52c41a",
                    "marker": "^",
                    "size": 18,
                    "alpha": 0.9,
                },
                {
                    "label": "Fill Sell",
                    "mask": lambda frame: (frame["event_type"] == "fill") & (frame["side"] == "sell"),
                    "color": "#f5222d",
                    "marker": "v",
                    "size": 18,
                    "alpha": 0.9,
                },
                {
                    "label": "Cancels",
                    "mask": lambda frame: frame["event_type"] == "cancel",
                    "color": "#8c8c8c",
                    "marker": "x",
                    "size": 14,
                    "alpha": 0.45,
                },
            ],
            x_lookup=x_lookup,
        )

        _plot_state_bands(
            ax5,
            record,
            [
                ("quoting_enabled", "quoting_enabled", None),
                ("wind_down", "wind_down", None),
                ("filter_suppress_bid", "suppress_bid", None),
                ("filter_suppress_ask", "suppress_ask", None),
                ("leg2_stale_warning", "leg2_stale", None),
            ],
            x_values=x,
        )
        ax5.set_xlabel("Time")

        for axis in axes:
            _shade_gap_regions(axis, gap_markers)
        _apply_compressed_time_axis(ax5, display_x, display_time, gap_markers)

        plt.tight_layout()

        date_str, session_type = self._infer_session_artifact_parts()
        output_directory = get_plot_dir(
            self.output_root or self._resolve_output_directory("."), self._infer_underlying_tag(), date_str, "overview"
        )
        file_name = f"session_{self.contract}_{date_str}_{session_type}.png"
        full_path = os.path.join(output_directory, file_name)
        plt.savefig(full_path, dpi=150)
        print(f"文件已成功保存到: {full_path}")
        plt.close(fig)

        interactive_needed = should_generate_interactive_plot(
            plot_family="overview",
            point_count=len(plot_record),
            subplot_count=5,
            trace_count=10
            + (int(events["reason_group"].nunique()) if not events.empty and "reason_group" in events.columns else 0),
            occlusion_flags=[len(plot_record) > 20_000, not events.empty and len(events) > 5_000],
        )
        if interactive_needed:
            interactive_fig = create_interactive_figure(
                rows=5,
                row_heights=[0.42, 0.18, 0.16, 0.11, 0.13],
                subplot_titles=[
                    f"{self.contract} · Price / fair / quote ladder",
                    "Strategy PnL layers (exact PNL_* definitions)",
                    "Position / exposure",
                    "Execution / cancel strip",
                    "Policy / state strip",
                ],
                title=f"Session overview · {self.contract}",
                height=1180,
            )

            if {"bid", "ask"}.issubset(record.columns):
                interactive_fig.add_trace(
                    go.Scatter(
                        x=record["datetime"],
                        y=record["ask"],
                        mode="lines",
                        line=dict(width=0),
                        showlegend=False,
                        hoverinfo="skip",
                    ),
                    row=1,
                    col=1,
                )
                interactive_fig.add_trace(
                    go.Scatter(
                        x=record["datetime"],
                        y=record["bid"],
                        mode="lines",
                        line=dict(width=0),
                        name="Market Spread",
                        fill="tonexty",
                        fillcolor="rgba(217,217,217,0.18)",
                        hoverinfo="skip",
                    ),
                    row=1,
                    col=1,
                )

            add_line_trace(
                interactive_fig,
                row=1,
                x=record["datetime"],
                y=plot_record["traded"],
                name="Traded",
                color="#262626",
                width=1.0,
            )
            if "fair_mid" in plot_record.columns:
                add_line_trace(
                    interactive_fig,
                    row=1,
                    x=record["datetime"],
                    y=plot_record["fair_mid"],
                    name="Fair Mid",
                    color="#fa8c16",
                    width=1.1,
                    dash="dash",
                )
            add_line_trace(
                interactive_fig,
                row=1,
                x=record["datetime"],
                y=plot_record["fair_ask"],
                name="Fair Ask",
                color="#faad14",
                width=1.0,
                opacity=0.55,
                visible="legendonly",
            )
            add_line_trace(
                interactive_fig,
                row=1,
                x=record["datetime"],
                y=plot_record["fair_bid"],
                name="Fair Bid",
                color="#d48806",
                width=1.0,
                opacity=0.55,
                visible="legendonly",
            )
            if "target_bid_px" in plot_record.columns:
                add_line_trace(
                    interactive_fig,
                    row=1,
                    x=record["datetime"],
                    y=plot_record["target_bid_px"],
                    name="Target Bid",
                    color="#0958d9",
                    width=1.0,
                    dash="dot",
                )
            if "target_ask_px" in plot_record.columns:
                add_line_trace(
                    interactive_fig,
                    row=1,
                    x=record["datetime"],
                    y=plot_record["target_ask_px"],
                    name="Target Ask",
                    color="#08979c",
                    width=1.0,
                    dash="dot",
                )
            add_line_trace(
                interactive_fig,
                row=1,
                x=record["datetime"],
                y=plot_record["cost"],
                name="Cost",
                color="#ff85c0",
                width=1.6,
                opacity=0.85,
            )
            if "best_resting_bid_px" in plot_record.columns:
                add_marker_trace(
                    interactive_fig,
                    row=1,
                    x=record["datetime"],
                    y=plot_record["best_resting_bid_px"],
                    name="Resting Bid",
                    color="#0958d9",
                    symbol="circle",
                    size=5,
                    opacity=0.45,
                    visible="legendonly",
                )
            if "best_resting_ask_px" in plot_record.columns:
                add_marker_trace(
                    interactive_fig,
                    row=1,
                    x=record["datetime"],
                    y=plot_record["best_resting_ask_px"],
                    name="Resting Ask",
                    color="#08979c",
                    symbol="circle",
                    size=5,
                    opacity=0.45,
                    visible="legendonly",
                )

            add_step_trace(
                interactive_fig,
                row=2,
                x=record["datetime"],
                y=plot_record[PNL_GROSS_COL],
                name=PNL_GROSS_COL,
                color="#52c41a",
                width=1.1,
            )
            add_step_trace(
                interactive_fig,
                row=2,
                x=record["datetime"],
                y=plot_record[PNL_AFT_TRADE_FEE_COL],
                name=PNL_AFT_TRADE_FEE_COL,
                color="#0958d9",
                width=1.05,
            )
            add_step_trace(
                interactive_fig,
                row=2,
                x=record["datetime"],
                y=plot_record[PNL_AFT_TRADE_FEE_MSG_COL],
                name=PNL_AFT_TRADE_FEE_MSG_COL,
                color="#531dab",
                width=1.05,
            )
            add_step_trace(
                interactive_fig,
                row=2,
                x=record["datetime"],
                y=plot_record[PNL_AFT_TRADE_FEE_PLUS_REBATE_COL],
                name=PNL_AFT_TRADE_FEE_PLUS_REBATE_COL,
                color="#d46b08",
                width=1.05,
            )
            add_step_trace(
                interactive_fig,
                row=2,
                x=record["datetime"],
                y=plot_record[PNL_NET_COL],
                name=PNL_NET_COL,
                color="#cf1322",
                width=1.35,
            )

            add_step_trace(
                interactive_fig,
                row=3,
                x=record["datetime"],
                y=plot_record["pos"],
                name="Leg Pos",
                color="#cf1322",
                width=1.1,
            )
            if "spread_pos" in plot_record.columns:
                add_step_trace(
                    interactive_fig,
                    row=3,
                    x=record["datetime"],
                    y=plot_record["spread_pos"],
                    name="Spread Pos",
                    color="#2f54eb",
                    width=1.0,
                )
            if "unhedged_exposure" in plot_record.columns:
                add_step_trace(
                    interactive_fig,
                    row=3,
                    x=record["datetime"],
                    y=plot_record["unhedged_exposure"],
                    name="Unhedged Exposure",
                    color="#fa8c16",
                    width=1.0,
                )
            if "hedge_need_qty" in plot_record.columns:
                add_step_trace(
                    interactive_fig,
                    row=3,
                    x=record["datetime"],
                    y=plot_record["hedge_need_qty"],
                    name="Hedge Need",
                    color="#722ed1",
                    width=1.0,
                    visible="legendonly",
                )

            event_specs = [
                {
                    "label": "Fill Buy",
                    "mask": lambda frame: (frame["event_type"] == "fill") & (frame["side"] == "buy"),
                    "color": "#52c41a",
                    "symbol": "triangle-up",
                    "size": 8,
                    "visible": True,
                },
                {
                    "label": "Fill Sell",
                    "mask": lambda frame: (frame["event_type"] == "fill") & (frame["side"] == "sell"),
                    "color": "#f5222d",
                    "symbol": "triangle-down",
                    "size": 8,
                    "visible": True,
                },
            ]
            if not events.empty:
                for reason_group in sorted(
                    events.loc[events["event_type"] == "cancel", "reason_group"].dropna().unique()
                ):
                    event_specs.append(
                        {
                            "label": f"Cancel · {reason_group}",
                            "mask": lambda frame, reason_group=reason_group: (
                                (frame["event_type"] == "cancel") & (frame["reason_group"] == reason_group)
                            ),
                            "color": _CANCEL_REASON_COLORS.get(reason_group, _CANCEL_REASON_COLORS["unknown"]),
                            "symbol": "x",
                            "size": 7,
                            "visible": "legendonly",
                            "opacity": 0.55,
                        }
                    )
            add_event_strip(interactive_fig, row=4, events=events, event_specs=event_specs)
            add_state_heatmap(
                interactive_fig,
                row=5,
                x=record["datetime"],
                frame=record,
                field_specs=[
                    ("quoting_enabled", "quoting_enabled", None),
                    ("wind_down", "wind_down", None),
                    ("filter_suppress_bid", "suppress_bid", None),
                    ("filter_suppress_ask", "suppress_ask", None),
                    ("leg2_stale_warning", "leg2_stale", None),
                ],
            )

            interactive_fig.update_yaxes(title_text="Price", row=1, col=1)
            interactive_fig.update_yaxes(title_text="PnL", row=2, col=1)
            interactive_fig.update_yaxes(title_text="Qty", row=3, col=1)
            interactive_fig.update_xaxes(title_text="Time", row=5, col=1)

            html_path = save_interactive_figure(interactive_fig, full_path)
            print(f"交互图已成功保存到: {html_path}")

    def save_record(self):
        if not self.session_record:
            print(f"No session record to save for {self.contract}.")
            return

        record = _prepare_strategy_record_frame(pd.DataFrame(self.session_record))
        record.set_index("datetime", inplace=True)
        date_str, session_type = self._infer_session_artifact_parts()
        file_name = f"session_record_{self.contract}_{date_str}_{session_type}.csv"
        output_directory = get_raw_dir(
            self.output_root or self._resolve_output_directory("."), self._infer_underlying_tag(), date_str, "strategy"
        )
        full_path = os.path.join(output_directory, file_name)
        os.makedirs(output_directory, exist_ok=True)

        record.to_csv(full_path, index=True)
        print(f"文件已成功保存到: {full_path}")

        if self.event_log:
            event_frame = pd.DataFrame(self.event_log)
            event_directory = get_raw_dir(
                self.output_root or self._resolve_output_directory("."),
                self._infer_underlying_tag(),
                date_str,
                "events",
            )
            event_path = os.path.join(event_directory, f"events_{self.contract}_{date_str}_{session_type}.csv")
            event_frame.to_csv(event_path, index=False)
            print(f"文件已成功保存到: {event_path}")

    def get_extra(self):
        bid_book = self.market.bids.get(getattr(self, "contract", None), {}) if self.market is not None else {}
        ask_book = self.market.asks.get(getattr(self, "contract", None), {}) if self.market is not None else {}

        best_resting_bid_px = max(bid_book) if bid_book else np.nan
        best_resting_ask_px = min(ask_book) if ask_book else np.nan
        best_resting_bid_qty = sum(order["qty"] for order in bid_book.get(best_resting_bid_px, [])) if bid_book else 0
        best_resting_ask_qty = -sum(order["qty"] for order in ask_book.get(best_resting_ask_px, [])) if ask_book else 0

        return {
            "best_resting_bid_px": best_resting_bid_px,
            "best_resting_ask_px": best_resting_ask_px,
            "best_resting_bid_qty": best_resting_bid_qty,
            "best_resting_ask_qty": best_resting_ask_qty,
            "resting_bid_levels": len(bid_book),
            "resting_ask_levels": len(ask_book),
            "trading_date": getattr(self, "trading_date", None),
            "prediction_policy_enabled": getattr(self, "prediction_policy_enabled", False),
            "prediction_diagnostics_only": getattr(self, "prediction_diagnostics_only", False),
            "pred_available": getattr(self, "pred_available", False),
            "pred_contract_1": getattr(self, "pred_contract_1", None),
            "pred_contract_2": getattr(self, "pred_contract_2", None),
            "pred_leg1_source_exchtime": getattr(self, "pred_leg1_source_exchtime", pd.NaT),
            "pred_leg2_source_exchtime": getattr(self, "pred_leg2_source_exchtime", pd.NaT),
            "pred_leg1_move": getattr(self, "pred_leg1_move", np.nan),
            "pred_leg2_move": getattr(self, "pred_leg2_move", np.nan),
            "pred_spread_move": getattr(self, "pred_spread_move", np.nan),
            "pred_leg1_ticks": getattr(self, "pred_leg1_ticks", np.nan),
            "pred_leg2_ticks": getattr(self, "pred_leg2_ticks", np.nan),
            "pred_spread_ticks": getattr(self, "pred_spread_ticks", np.nan),
            "pred_leg1_age_ms": getattr(self, "pred_leg1_age_ms", np.nan),
            "pred_leg2_age_ms": getattr(self, "pred_leg2_age_ms", np.nan),
            "pred_leg1_state": getattr(self, "pred_leg1_state", None),
            "pred_leg2_state": getattr(self, "pred_leg2_state", None),
            "pred_spread_state": getattr(self, "pred_spread_state", None),
            "pred_sign_persistence_window": getattr(self, "pred_sign_persistence_window", 0),
            "pred_leg1_sign": getattr(self, "pred_leg1_sign", 0),
            "pred_leg2_sign": getattr(self, "pred_leg2_sign", 0),
            "pred_spread_sign": getattr(self, "pred_spread_sign", 0),
            "pred_leg1_same_sign_count": getattr(self, "pred_leg1_same_sign_count", 0),
            "pred_leg2_same_sign_count": getattr(self, "pred_leg2_same_sign_count", 0),
            "pred_spread_same_sign_count": getattr(self, "pred_spread_same_sign_count", 0),
        }
