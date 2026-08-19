"""Acceptance coverage for the public foundation taker-style example."""

from __future__ import annotations

import inspect
from pathlib import Path
from tempfile import TemporaryDirectory

from common.telemetry import load_canonical_table
from examples.foundation_taker import demo, taker_policy


def test_foundation_taker_example_uses_the_supported_replay_path() -> None:
    source = inspect.getsource(demo) + inspect.getsource(taker_policy)
    assert "public_tools" not in source
    assert "common.production_replay" in source

    with TemporaryDirectory() as temporary:
        result = demo.run_demo(Path(temporary))
        assert result.telemetry.eligible
        assert len(result.decision_ids) == 3
        assert len(result.execution_ids) == 1
        assert result.passive_fill_ids == ()
        assert not result.economics_eligible

        run_dir = Path(temporary) / "foundation-taker-demo"
        hedge_orders = [
            row
            for row in load_canonical_table(run_dir, "orders")
            if row["record_type"] == "order_declared" and row["role"] == "hedge"
        ]
        assert len(hedge_orders) == 1
        pricing_reference = hedge_orders[0]["pricing_reference"]
        assert pricing_reference["basis"] == "post_batch_snapshot_v1"
        assert pricing_reference["trigger_fill_id"] is None

        decisions = {row["decision_id"]: row for row in load_canonical_table(run_dir, "decisions")}
        decision = decisions[hedge_orders[0]["decision_id"]]
        assert pricing_reference["pricing_batch_id"] == decision["exchange_batch_id"]
        assert pricing_reference["pricing_batch_seq"] == decision["exchange_batch_seq"]
        assert pricing_reference["pricing_snapshot_id"] == decision["hedge_book_snapshot_id"]
