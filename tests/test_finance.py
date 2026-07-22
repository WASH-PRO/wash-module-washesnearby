"""Unit tests for CRM → Owner finance mapping (inlined in main.py for PyOrch)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from main import (
    bucket_from_crm_row,
    build_finance_payload,
    empty_bucket,
    local_finance_date,
    money,
    sub_buckets,
    sum_buckets,
)


def ref_id(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("id") or value.get("_id") or "")
    return str(value)


def stats_for(wash_id, post_id, before, after, recorded_at):
    return [
        {
            "washId": wash_id,
            "postId": post_id,
            "period": "before_collection",
            "cash": before[0],
            "cashless": before[1],
            "discountOps": before[2],
            "recordedAt": recorded_at,
        },
        {
            "washId": wash_id,
            "postId": post_id,
            "period": "after_collection",
            "cash": after[0],
            "cashless": after[1],
            "discountOps": after[2],
            "recordedAt": recorded_at,
        },
    ]


class FinanceMappingTests(unittest.TestCase):
    def test_money_and_bucket_mapping(self):
        self.assertEqual(money(-5), 0.0)
        self.assertEqual(money("12.345"), 12.35)
        bucket = bucket_from_crm_row(
            {"cash": 100, "cashless": 200, "discountOps": 30}
        )
        self.assertEqual(bucket, {"cash": 100.0, "external": 200.0, "discount": 30.0})

    def test_sum_and_sub_buckets(self):
        a = {"cash": 10, "external": 20, "discount": 1}
        b = {"cash": 3, "external": 5, "discount": 1}
        self.assertEqual(sum_buckets([a, b])["cash"], 13.0)
        self.assertEqual(sub_buckets(a, b), {"cash": 7.0, "external": 15.0, "discount": 0.0})

    def test_local_finance_date_yekaterinburg(self):
        now = datetime(2026, 7, 21, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(local_finance_date("Asia/Yekaterinburg", now), "2026-07-22")

    def test_omits_when_empty(self):
        self.assertIsNone(
            build_finance_payload(
                "wash1",
                [{"id": "p1", "postNumber": 1}],
                [],
                {},
                finance_date="2026-07-22",
                ref_id=ref_id,
            )
        )

    def test_skips_other_days(self):
        posts = [{"id": "p1", "postNumber": 1, "washId": "wash1"}]
        stats = stats_for("wash1", "p1", (100, 50, 10), (5000, 8000, 400), "2026-07-21T10:00:00Z")
        self.assertIsNone(
            build_finance_payload(
                "wash1",
                posts,
                stats,
                {},
                finance_date="2026-07-22",
                finance_timezone="UTC",
                ref_id=ref_id,
            )
        )

    def test_today_equals_before_even_when_after_lower(self):
        """Real CRM case: before.cashless can be > after.cashless — today must follow before."""
        posts = [{"id": "p1", "postNumber": 1, "washId": "wash1"}]
        # Mirrors Хохрякова live counters
        stats = stats_for(
            "wash1",
            "p1",
            (0, 321089, 300),
            (474052, 57735, 238),
            "2026-07-22T10:00:00Z",
        )
        state: dict = {}
        first = build_finance_payload(
            "wash1",
            posts,
            stats,
            state,
            finance_date="2026-07-22",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        assert first is not None
        self.assertEqual(
            first["today"],
            {"cash": 0.0, "external": 321089.0, "discount": 300.0},
        )
        self.assertEqual(first["before_collection"]["external"], 321089.0)
        self.assertEqual(first["after_collection"]["cash"], 474052.0)

        # Growth of current period
        stats = stats_for(
            "wash1",
            "p1",
            (0, 322000, 300),
            (474052, 58000, 238),
            "2026-07-22T12:00:00Z",
        )
        second = build_finance_payload(
            "wash1",
            posts,
            stats,
            state,
            finance_date="2026-07-22",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        assert second is not None
        self.assertEqual(second["today"]["external"], 322000.0)

    def test_collection_mid_day_banks_previous_before(self):
        posts = [{"id": "p1", "postNumber": 1, "washId": "wash1"}]
        state: dict = {}
        first = build_finance_payload(
            "wash1",
            posts,
            stats_for("wash1", "p1", (100, 200, 10), (1000, 2000, 50), "2026-07-22T10:00:00Z"),
            state,
            finance_date="2026-07-22",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        assert first is not None
        self.assertEqual(first["today"], {"cash": 100.0, "external": 200.0, "discount": 10.0})

        # Инкассация: before resets, after stays / grows
        after_collect = build_finance_payload(
            "wash1",
            posts,
            stats_for("wash1", "p1", (5, 20, 0), (1005, 2020, 50), "2026-07-22T15:00:00Z"),
            state,
            finance_date="2026-07-22",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        assert after_collect is not None
        self.assertEqual(
            after_collect["today"],
            {"cash": 105.0, "external": 220.0, "discount": 10.0},
        )

    def test_new_day_resets_without_yesterday(self):
        posts = [{"id": "p1", "postNumber": 1, "washId": "wash1"}]
        state: dict = {}
        build_finance_payload(
            "wash1",
            posts,
            stats_for("wash1", "p1", (100, 200, 10), (1000, 2000, 50), "2026-07-22T10:00:00Z"),
            state,
            finance_date="2026-07-22",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        nxt = build_finance_payload(
            "wash1",
            posts,
            stats_for("wash1", "p1", (40, 50, 1), (1040, 2050, 51), "2026-07-23T08:00:00Z"),
            state,
            finance_date="2026-07-23",
            finance_timezone="UTC",
            ref_id=ref_id,
        )
        assert nxt is not None
        self.assertEqual(nxt["today"], {"cash": 40.0, "external": 50.0, "discount": 1.0})
        self.assertEqual(state["financeBaseline"]["date"], "2026-07-23")


if __name__ == "__main__":
    unittest.main()
