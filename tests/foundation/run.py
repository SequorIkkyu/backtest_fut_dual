"""Zero-dependency runner for Phase-0 foundation acceptance tests."""

from __future__ import annotations

import importlib
import sys
import traceback


MODULES = (
    "test_contract_vocabulary",
    "test_phase1_session_lifecycle",
    "test_phase2_causal_ingress",
    "test_phase3_execution",
    "test_phase4a_lifecycle",
    "test_phase4b_ledger",
    "test_phase5_telemetry",
    "test_phase4c_pnl_attribution",
    "test_phase6_foundation_api",
    "test_phase7_hardening",
    "test_phase8_operational",
    "test_phase8_research_telemetry",
    "test_phase8_production_replay",
    "test_phase9_raw_snapshot_adapter",
    "test_example_foundation_taker",
    "test_legacy_characterization",
)


def main() -> int:
    passed = 0
    failures: list[tuple[str, str, BaseException]] = []
    for mod_name in MODULES:
        module = importlib.import_module(f"common.tests.foundation.{mod_name}")
        test_names = sorted(name for name in dir(module) if name.startswith("test_") and callable(getattr(module, name)))
        print(f"\nfoundation.{mod_name}  ({len(test_names)} tests)")
        for name in test_names:
            try:
                getattr(module, name)()
            except Exception as exc:  # noqa: BLE001 - report all acceptance failures
                failures.append((mod_name, name, exc))
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            else:
                passed += 1
                print(f"  ok    {name}")
    total = passed + len(failures)
    print(f"\n{'=' * 60}\n{passed}/{total} passed, {len(failures)} failed")
    for mod_name, name, exc in failures:
        print(f"\n----- foundation.{mod_name}.{name} -----")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
