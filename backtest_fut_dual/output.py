"""Standard output layout for strategy backtests.

Every strategy writes under one `OUTPUT` root with the same subdirectories and
file-naming convention (the taker layout, adopted everywhere):

    OUTPUT/
      summary/      {UND}_{tag}_grid_search.csv, {UND}_summary.csv
      cycle/        {UND}_cycles.csv
      diagnostics/  {UND}_regime_diagnostics.csv
      plots/        {UND}_*.png / *.html
      best_config/  {UND}_{tag}_best_config.py
"""

from __future__ import annotations

import os

SUBDIRS = ("summary", "cycle", "diagnostics", "plots", "best_config")


def ensure_output_dirs(output_root: str) -> str:
    for sub in SUBDIRS:
        os.makedirs(os.path.join(output_root, sub), exist_ok=True)
    return output_root


def _join(output_root: str, *parts: str) -> str:
    return os.path.join(output_root, *parts)


def grid_search_path(output_root: str, und: str, tag: str) -> str:
    return _join(output_root, "summary", f"{und}_{tag}_grid_search.csv")


def summary_path(output_root: str, und: str) -> str:
    return _join(output_root, "summary", f"{und}_summary.csv")


def cycles_path(output_root: str, und: str) -> str:
    return _join(output_root, "cycle", f"{und}_cycles.csv")


def diagnostics_path(output_root: str, und: str) -> str:
    return _join(output_root, "diagnostics", f"{und}_regime_diagnostics.csv")


def order_limit_path(output_root: str, und: str) -> str:
    return _join(output_root, "diagnostics", f"{und}_order_limit.csv")


def best_config_path(output_root: str, und: str, tag: str) -> str:
    return _join(output_root, "best_config", f"{und}_{tag}_best_config.py")


def run_manifest_path(output_root: str, und: str, tag: str) -> str:
    return _join(output_root, f"{und}_{tag}_run_manifest.json")


def plot_path(output_root: str, und: str, name: str = "backtest", ext: str = "png") -> str:
    return _join(output_root, "plots", f"{und}_{name}.{ext}")


def summary_log_path(output_root: str, und: str) -> str:
    return _join(output_root, f"{und}_summary.txt")
