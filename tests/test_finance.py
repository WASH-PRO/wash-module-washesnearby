"""Unit tests for CRM → Owner finance mapping."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from finance import (
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


class FinanceMappingTests(unittest.TestCase):
    def test_money_and_bucket_mapping(self):
        self.assertEqual(money(-5), 0.0)
        self.assertEqual(money("12.345"), 12.35)
        bucket = bucket_from_crm_row(
            {"cash": 100, "cashless": 200, "discountOps": 30}
        )
        self.assertEqual(bucket, {"cash": 100.0, "external": 200.0, "discount": 30.0})
        legacy = bucket_from_crm_row({"cash": 1, "card": 2, "discount": 3})
        self.assertEqual(legacy["external"], 2.0)
        self.assertEqual(legacy["discount"], 3.0)

    def test_sum_and_sub_buckets(self):
        a = {"cash": 10, "external": 20, "discount": 1}
        b = {"cash": 3, "external": 5, "discount": 1}
        self.assertEqual(sum_buckets([a, b])["cash"], 13.0)
        self.assertEqual(sub_buckets(a, b), {"cash": 7.0, "external": 15.0, "discount": 0.0})
        self.assertEqual(sub_buckets(b, a)["cash"], 0.0)

    def test_local_finance_date_yekaterinburg(self):
        # 2026-07-21 22:30 UTC → 2026-07-22 03:30 in Asia/Yekaterinburg (UTC+5)
        now = datetime(2026, 7, 21, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(local_finance_date("Asia/Yekaterinburg", now), "2026-07-22")

    def test_build_finance_payload_omits_when_empty(self):
        state: dict = {}
        payload = build_finance_payload(
            "wash1",
            [{"id": "p1", "postNumber": 1}],
            [],
            state,
            finance_date="2026-07-22",
            ref_id=ref_id,
        )
        self.assertIsNone(payload)

    def test_build_finance_payload_today_delta(self):
        posts = [
            {"id": "p1", "postNumber": 1, "washId": "wash1"},
            {"id": "p2", "postNumber": 2, "washId": "wash1"},
        ]
        stats = [
            {
                "washId": "wash1",
                "postId": "p1",
                "period": "before_collection",
                "cash": 100,
                "cashless": 50,
                "discountOps": 10,
                "recordedAt": "2026-07-22T10:00:00Z",
            },
            {
                "washId": "wash1",
                "postId": "p1",
                "period": "after_collection",
                "cash": 5000,
                "cashless": 8000,
                "discountOps": 400,
                "recordedAt": "2026-07-22T10:00:00Z",
            },
            {
                "washId": "wash1",
                "postId": "p2",
                "period": "before_collection",
                "cash": 20,
                "cashless": 30,
                "discountOps": 5,
                "recordedAt": "2026-07-22T10:00:00Z",
            },
            {
                "washId": "wash1",
                "postId": "p2",
                "period": "after_collection",
                "cash": 1000,
                "cashless": 2000,
                "discountOps": 100,
                "recordedAt": "2026-07-22T10:00:00Z",
            },
        ]
        state: dict = {}

        first = build_finance_payload(
            "wash1", posts, stats, state, finance_date="2026-07-22", ref_id=ref_id
        )
        assert first is not None
        self.assertEqual(first["date"], "2026-07-22")
        self.assertEqual(first["today"], empty_bucket())
        self.assertEqual(first["before_collection"]["cash"], 120.0)
        self.assertEqual(first["after_collection"]["external"], 10000.0)
        self.assertEqual(len(first["posts"]), 2)
        self.assertEqual(state["financeBaseline"]["date"], "2026-07-22")

        # Growth on same day → today delta from after_collection
        stats[1] = {
            **stats[1],
            "cash": 5200,
            "cashless": 8300,
            "discountOps": 450,
            "recordedAt": "2026-07-22T12:00:00Z",
        }
        second = build_finance_payload(
            "wash1", posts, stats, state, finance_date="2026-07-22", ref_id=ref_id
        )
        assert second is not None
        self.assertEqual(
            second["today"],
            {"cash": 200.0, "external": 300.0, "discount": 50.0},
        )
        self.assertEqual(second["posts"][0]["today"]["cash"], 200.0)
        self.assertEqual(second["posts"][1]["today"], empty_bucket())

        # New calendar day re-baselines today to zero
        third = build_finance_payload(
            "wash1", posts, stats, state, finance_date="2026-07-23", ref_id=ref_id
        )
        assert third is not None
        self.assertEqual(third["date"], "2026-07-23")
        self.assertEqual(third["today"], empty_bucket())
        self.assertEqual(state["financeBaseline"]["date"], "2026-07-23")


if __name__ == "__main__":
    unittest.main()
