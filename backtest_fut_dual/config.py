"""Config contract shared by all strategy drivers.

Defines the canonical config keys every strategy config should provide, a light
validator, and the opt-in diagnostics flag set ("core + opt-in extras").
"""

from __future__ import annotations

# Keys every strategy config must define.
REQUIRED_KEYS = ("UND", "MULT", "TICK", "OUTPUT", "REBATE")
# At least one of these fee specs must be present (FEE rate or FEE_LOT fixed $/lot).
FEE_KEYS = ("FEE", "FEE_LOT")
# Commonly expected, warned-if-absent (signal-driven strategies need SIGNAL; arb
# auto-detects contracts instead, so SIGNAL is only recommended, not required).
RECOMMENDED_KEYS = ("SIGNAL", "MD_PATH", "CUTOFF", "CONTRACT_UNIVERSE")

# Opt-in diagnostics modules (core reporting always runs; these are extra).
KNOWN_DIAGNOSTICS = (
    "regime_split",      # display_regime_diagnostics (peak-drawdown regime split)
    "tod_buckets",       # time-of-day PnL-per-qty buckets
    "monthly_pnl",       # monthly PnL aggregates
    "microstructure",    # entry-side depth / opposite_volume analysis (taker)
    "interactive_plots", # plotly HTML in addition to PNG
    "best_config",       # write best-combo config file
    "order_limit",       # per-(contract, day) order-message breach report (auto-runs when enabled)
)

# Order-message limit feature (per contract per trading day). Read by common.grid
# (ns.get with these defaults); set in a config/driver to override.
ORDER_MSG_LIMIT_DEFAULT = 4000
ORDER_LIMIT_ENABLED_DEFAULT = True


def validate_config(ns: dict) -> list[str]:
    """Return a list of problem strings (empty == valid). Non-raising so a driver
    can warn-and-continue or assert as it prefers."""
    problems: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in ns:
            problems.append(f"missing required config key: {key}")
    if not any(k in ns and ns[k] is not None for k in FEE_KEYS):
        problems.append(f"config must define one of {FEE_KEYS} (a fee rate or fixed $/lot)")
    for key in RECOMMENDED_KEYS:
        if key not in ns:
            problems.append(f"(warning) recommended config key absent: {key}")
    return problems


def resolve_diagnostics(ns: dict) -> set[str]:
    """Read the opt-in DIAGNOSTICS flag list from a config namespace.

    Accepts a list/tuple/set of flag names, the string 'all', or absence (== core
    only). Unknown flags are dropped (silently ignored).
    """
    value = ns.get("DIAGNOSTICS")
    if value is None:
        return set()
    if isinstance(value, str):
        if value.lower() == "all":
            return set(KNOWN_DIAGNOSTICS)
        value = [value]
    return {flag for flag in value if flag in KNOWN_DIAGNOSTICS}
