from __future__ import annotations

import os
from typing import Callable

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


HIGH_DENSITY_POINT_THRESHOLD = 10_000
HIGH_DENSITY_TRACE_THRESHOLD = 8
INTERACTIVE_TRACE_MAX_POINTS = 6_000


def _uses_compressed_time_gaps(fig: go.Figure) -> bool:
    meta = fig.layout.meta
    return isinstance(meta, dict) and bool(meta.get("compress_time_gaps", False))


def should_generate_interactive_plot(
    *,
    plot_family: str,
    point_count: int,
    subplot_count: int,
    trace_count: int,
    occlusion_flags: list[bool] | tuple[bool, ...] | None = None,
) -> bool:
    """Decide whether a figure deserves an interactive HTML companion.

    The policy deliberately favors session / overview figures, where dense time
    series, multiple synchronized panes, and repeated zoom-in inspection are all
    common. Simpler dashboards remain static PNGs.
    """

    if plot_family in {"overview", "session"}:
        return True

    flags = list(occlusion_flags or [])
    return any(
        [
            point_count >= HIGH_DENSITY_POINT_THRESHOLD,
            subplot_count >= 3,
            trace_count >= HIGH_DENSITY_TRACE_THRESHOLD,
            any(flags),
        ]
    )


def interactive_html_path(static_path: str) -> str:
    base, _ = os.path.splitext(static_path)
    return f"{base}_interactive.html"


def save_interactive_figure(fig: go.Figure, static_path: str) -> str:
    html_path = interactive_html_path(static_path)
    os.makedirs(os.path.dirname(html_path), exist_ok=True)
    fig.write_html(html_path, include_plotlyjs=True, full_html=True)
    return html_path


def create_interactive_figure(
    *,
    rows: int,
    row_heights: list[float],
    subplot_titles: list[str],
    title: str,
    height: int,
    compress_time_gaps: bool = False,
) -> go.Figure:
    fig = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )
    fig.update_layout(
        title=title,
        template="plotly_white",
        height=height,
        hovermode="x unified",
        uirevision=title,
        meta={"compress_time_gaps": compress_time_gaps},
        margin=dict(l=60, r=205, t=118, b=40),
        hoverlabel=dict(align="left"),
        legend=dict(
            orientation="v",
            yanchor="top",
            y=1.095,
            xanchor="left",
            x=1.005,
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor="#d9d9d9",
            borderwidth=1,
            font=dict(size=10),
            tracegroupgap=4,
            itemclick="toggle",
            itemdoubleclick="toggleothers",
        ),
    )
    for row in range(1, rows + 1):
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", row=row, col=1)
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.06), row=rows, col=1)
    return fig


def downsample_timeframe(
    frame: pd.DataFrame,
    *,
    x_col: str,
    y_cols: list[str] | tuple[str, ...],
    max_points: int = INTERACTIVE_TRACE_MAX_POINTS,
    preserve_x=None,
) -> pd.DataFrame:
    if frame.empty or x_col not in frame.columns or len(frame) <= max_points:
        return frame.copy()

    value_cols = [column for column in y_cols if column in frame.columns]
    if not value_cols:
        return frame.copy()

    prepared = frame.reset_index(drop=True).copy()
    nan_rows = prepared[value_cols].isna().any(axis=1)
    selected: set[int] = {0, len(prepared) - 1, *prepared.index[nan_rows].tolist()}

    if preserve_x is not None:
        preserve_values = pd.Series(preserve_x).dropna()
        if not preserve_values.empty:
            selected.update(prepared.index[prepared[x_col].isin(preserve_values.unique())].tolist())

    remaining_budget = max(max_points - len(selected), 1)
    bucket_size = max(8, int(np.ceil(len(prepared) / max(remaining_budget // max(2, len(value_cols) + 1), 1))))

    for start in range(0, len(prepared), bucket_size):
        stop = min(start + bucket_size, len(prepared))
        bucket = prepared.iloc[start:stop]
        if bucket.empty:
            continue
        selected.add(start)
        selected.add(stop - 1)
        for column in value_cols:
            numeric = pd.to_numeric(bucket[column], errors="coerce")
            valid = numeric.dropna()
            if valid.empty:
                continue
            selected.add(int(valid.idxmin()))
            selected.add(int(valid.idxmax()))

    return prepared.iloc[sorted(selected)].copy()


def _compress_step_xy(
    x,
    y,
    *,
    max_points: int = INTERACTIVE_TRACE_MAX_POINTS,
) -> tuple[pd.Series, pd.Series]:
    x_series = pd.Series(x).reset_index(drop=True)
    y_series = pd.Series(y).reset_index(drop=True)

    if len(y_series) <= 2:
        return x_series, y_series

    keep_mask = y_series.ne(y_series.shift()) | y_series.ne(y_series.shift(-1)) | y_series.isna()
    keep_mask.iloc[0] = True
    keep_mask.iloc[-1] = True

    compressed = pd.DataFrame({"x": x_series, "y": y_series}).loc[keep_mask].reset_index(drop=True)
    if len(compressed) > max_points:
        compressed = downsample_timeframe(compressed, x_col="x", y_cols=["y"], max_points=max_points)
    return compressed["x"], compressed["y"]


def add_line_trace(
    fig: go.Figure,
    *,
    row: int,
    x,
    y,
    name: str,
    color: str,
    width: float = 1.2,
    dash: str | None = None,
    opacity: float = 1.0,
    visible: bool | str = True,
    hover_suffix: str = "",
    use_gl: bool = True,
    fill: str | None = None,
    fillcolor: str | None = None,
    hover: bool = True,
    max_points: int = INTERACTIVE_TRACE_MAX_POINTS,
    preserve_x=None,
) -> None:
    cleaned = pd.Series(y)
    trace_frame = pd.DataFrame({"x": pd.Series(x), "y": cleaned})
    trace_frame = downsample_timeframe(trace_frame, x_col="x", y_cols=["y"], max_points=max_points, preserve_x=preserve_x)
    trace_cls = go.Scatter if _uses_compressed_time_gaps(fig) or not use_gl else go.Scattergl
    trace_kwargs = {
        "x": trace_frame["x"],
        "y": trace_frame["y"],
        "mode": "lines",
        "name": name,
        "visible": visible,
        "opacity": opacity,
        "fill": fill,
        "fillcolor": fillcolor,
        "line": {k: v for k, v in {"color": color, "width": width, "dash": dash}.items() if v is not None},
    }
    if hover:
        trace_kwargs["hovertemplate"] = f"%{{x|%Y-%m-%d %H:%M:%S.%L}}<br>{name}: %{{y}}{hover_suffix}<extra></extra>"
    else:
        trace_kwargs["hoverinfo"] = "skip"

    trace = trace_cls(**trace_kwargs)
    fig.add_trace(trace, row=row, col=1)


def add_step_trace(
    fig: go.Figure,
    *,
    row: int,
    x,
    y,
    name: str,
    color: str,
    width: float = 1.0,
    opacity: float = 1.0,
    visible: bool | str = True,
    hover_suffix: str = "",
    hover: bool = True,
    max_points: int = INTERACTIVE_TRACE_MAX_POINTS,
) -> None:
    compressed_x, compressed_y = _compress_step_xy(x, y, max_points=max_points)
    trace_kwargs = {
        "x": compressed_x,
        "y": compressed_y,
        "mode": "lines",
        "name": name,
        "visible": visible,
        "opacity": opacity,
        "line": dict(color=color, width=width, shape="hv"),
    }
    if hover:
        trace_kwargs["hovertemplate"] = f"%{{x|%Y-%m-%d %H:%M:%S.%L}}<br>{name}: %{{y}}{hover_suffix}<extra></extra>"
    else:
        trace_kwargs["hoverinfo"] = "skip"

    fig.add_trace(
        go.Scatter(**trace_kwargs),
        row=row,
        col=1,
    )


def add_marker_trace(
    fig: go.Figure,
    *,
    row: int,
    x,
    y,
    name: str,
    color: str,
    symbol: str,
    size: int = 7,
    opacity: float = 0.8,
    visible: bool | str = True,
    hover_fields: list[tuple[str, object]] | None = None,
) -> None:
    customdata = None
    hover_lines = ["%{x|%Y-%m-%d %H:%M:%S.%L}", f"{name}: %{{y}}"]
    if hover_fields:
        customdata = np.column_stack([np.asarray(values) for _, values in hover_fields])
        hover_lines.extend([f"{label}: %{{customdata[{idx}]}}" for idx, (label, _) in enumerate(hover_fields)])
    trace_cls = go.Scatter if _uses_compressed_time_gaps(fig) else go.Scattergl
    fig.add_trace(
        trace_cls(
            x=x,
            y=y,
            mode="markers",
            name=name,
            visible=visible,
            opacity=opacity,
            customdata=customdata,
            marker=dict(color=color, symbol=symbol, size=size),
            hovertemplate="<br>".join(hover_lines) + "<extra></extra>",
        ),
        row=row,
        col=1,
    )


def build_time_rangebreaks(
    x_values,
    *,
    gap_threshold: pd.Timedelta = pd.Timedelta(minutes=5),
    edge_buffer: pd.Timedelta = pd.Timedelta(milliseconds=1),
) -> list[dict[str, object]]:
    timestamps = pd.Series(pd.to_datetime(x_values)).dropna().sort_values().drop_duplicates().reset_index(drop=True)
    if timestamps.empty:
        return []

    rangebreaks: list[dict[str, object]] = []
    for idx in range(1, len(timestamps)):
        previous_time = timestamps.iloc[idx - 1]
        current_time = timestamps.iloc[idx]
        gap = current_time - previous_time
        if gap <= gap_threshold:
            continue

        hidden_gap = gap - edge_buffer * 2
        if hidden_gap <= pd.Timedelta(0):
            continue

        hidden_gap_ms = max(1, int(hidden_gap.total_seconds() * 1000))
        rangebreaks.append(
            {
                "values": [previous_time + edge_buffer],
                "dvalue": hidden_gap_ms,
            }
        )

    return rangebreaks


def apply_time_rangebreaks(
    fig: go.Figure,
    *,
    rows: int,
    x_values,
    gap_threshold: pd.Timedelta = pd.Timedelta(minutes=5),
) -> None:
    rangebreaks = build_time_rangebreaks(x_values, gap_threshold=gap_threshold)
    if not rangebreaks:
        return

    for row in range(1, rows + 1):
        fig.update_xaxes(type="date", rangebreaks=rangebreaks, row=row, col=1)


def add_state_heatmap(
    fig: go.Figure,
    *,
    row: int,
    x,
    frame: pd.DataFrame,
    field_specs: list[tuple[str, str, Callable[[pd.Series], np.ndarray] | None]],
) -> None:
    z_rows: list[np.ndarray] = []
    labels: list[str] = []
    for field_name, label, transform in field_specs:
        if field_name not in frame.columns:
            continue
        series = frame[field_name]
        values = transform(series) if transform is not None else series.fillna(False).astype(bool).astype(int).to_numpy()
        z_rows.append(np.asarray(values, dtype=float))
        labels.append(label)

    if not z_rows:
        return

    fig.add_trace(
        go.Heatmap(
            x=x,
            y=labels,
            z=np.vstack(z_rows),
            zmin=0,
            zmax=1,
            showscale=False,
            colorscale=[[0.0, "#f0f0f0"], [1.0, "#52c41a"]],
            hovertemplate="%{x|%Y-%m-%d %H:%M:%S.%L}<br>%{y}: %{z}<extra></extra>",
        ),
        row=row,
        col=1,
    )
    fig.update_yaxes(type="category", row=row, col=1)


def add_event_strip(
    fig: go.Figure,
    *,
    row: int,
    events: pd.DataFrame,
    event_specs: list[dict[str, object]],
) -> None:
    lane_map: dict[int, str] = {}
    for index, spec in enumerate(event_specs, start=1):
        lane_id = int(spec.get("lane", index))
        lane_map.setdefault(lane_id, str(spec.get("lane_label", spec["label"])))

    if not lane_map:
        return

    tickvals = list(lane_map.keys())
    ticktext = list(lane_map.values())
    for index, spec in enumerate(event_specs, start=1):
        lane_id = int(spec.get("lane", index))
        mask = spec["mask"](events)
        subset = events.loc[mask].copy()
        if subset.empty:
            continue
        hover_fields = []
        for field in ("contract", "event_type", "side", "qty", "price", "reason_code", "hedge_episode_id", "requote_count"):
            if field in subset.columns:
                hover_fields.append((field, subset[field].astype(str).to_numpy()))
        add_marker_trace(
            fig,
            row=row,
            x=subset["event_time"],
            y=np.full(len(subset), lane_id),
            name=str(spec["label"]),
            color=str(spec["color"]),
            symbol=str(spec.get("symbol", "circle")),
            size=int(spec.get("size", 7)),
            opacity=float(spec.get("opacity", 0.85)),
            visible=spec.get("visible", True),
            hover_fields=hover_fields,
        )

    fig.update_yaxes(
        row=row,
        col=1,
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        range=[min(tickvals) - 0.5, max(tickvals) + 0.5],
    )
