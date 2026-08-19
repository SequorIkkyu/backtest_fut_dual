from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

from common.sessions import classify_session


def ensure_directory(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def infer_session_artifact_parts(first_dt) -> tuple[str, str]:
    if first_dt is None:
        return "unknown", "mixed"

    ts = pd.Timestamp(first_dt)
    date_str = ts.strftime("%Y%m%d")
    return date_str, classify_session(ts.hour)


def infer_underlying_tag(*contracts: str | None, fallback: str | None = None) -> str:
    for contract in contracts:
        if not contract:
            continue
        match = re.match(r"[A-Za-z]+", str(contract))
        if match:
            return match.group(0).lower()
    return (fallback or "unknown").lower()


def build_session_tag(*parts: str | None) -> str:
    return "_".join(str(part) for part in parts if part)


def get_single_day_root(output_root: str, underlying: str, date_str: str) -> str:
    return os.path.join(os.path.abspath(output_root), "single_day", underlying, date_str)


def get_raw_dir(output_root: str, underlying: str, date_str: str, *parts: str) -> str:
    return ensure_directory(os.path.join(get_single_day_root(output_root, underlying, date_str), "raw", *parts))


def get_plot_dir(output_root: str, underlying: str, date_str: str, *parts: str) -> str:
    return ensure_directory(os.path.join(get_single_day_root(output_root, underlying, date_str), "plots", *parts))


def get_root_plot_dir(output_root: str, *parts: str) -> str:
    return ensure_directory(os.path.join(os.path.abspath(output_root), "plots", *parts))


def get_bundle_overview(output_root: str, underlying: str, date_str: str) -> dict[str, str]:
    root = get_single_day_root(output_root, underlying, date_str)
    ensure_directory(root)
    raw_root = get_raw_dir(output_root, underlying, date_str)
    plot_root = get_plot_dir(output_root, underlying, date_str)
    return {
        "root": root,
        "raw": raw_root,
        "plots": plot_root,
    }


def list_existing_paths(paths: Iterable[str]) -> list[str]:
    return [str(Path(path)) for path in paths if os.path.exists(path)]
