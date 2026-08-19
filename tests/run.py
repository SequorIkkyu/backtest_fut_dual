"""Zero-dependency test runner for the common/ engine suite.

pytest is not required: this discovers every ``test_*`` function in the suite
modules, runs each, and reports pass/fail with tracebacks. The same files are
plain-``assert`` pytest test functions, so ``pytest common/tests`` also works if
pytest is installed.

Run (py310, PYTHONPATH = <repo-root>;<repo-root>\strategies):

    python -m common.tests.run
"""

from __future__ import annotations

import importlib
import sys
import traceback

# Test modules in run order (fast/pure first, engine last).
MODULES = (
    "test_sessions",
    "test_cycles",
    "test_update_pos",
    "test_market",
)


def main() -> int:
    passed = 0
    failures: list[tuple[str, str, BaseException]] = []

    for mod_name in MODULES:
        module = importlib.import_module(f"common.tests.{mod_name}")
        test_names = sorted(n for n in dir(module) if n.startswith("test_") and callable(getattr(module, n)))
        print(f"\n{mod_name}  ({len(test_names)} tests)")
        for name in test_names:
            try:
                getattr(module, name)()
            except Exception as exc:                       # noqa: BLE001 - report any failure
                failures.append((mod_name, name, exc))
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            else:
                passed += 1
                print(f"  ok    {name}")

    total = passed + len(failures)
    print(f"\n{'=' * 60}\n{passed}/{total} passed, {len(failures)} failed")

    for mod_name, name, exc in failures:
        print(f"\n----- {mod_name}.{name} -----")
        traceback.print_exception(type(exc), exc, exc.__traceback__)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
