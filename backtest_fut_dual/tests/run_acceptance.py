"""Run the legacy common suite and the separate foundation acceptance suite."""

from __future__ import annotations

import sys

from common.tests import run as common_runner
from common.tests.foundation import run as foundation_runner


def main() -> int:
    common_status = common_runner.main()
    foundation_status = foundation_runner.main()
    return 1 if common_status or foundation_status else 0


if __name__ == "__main__":
    sys.exit(main())
