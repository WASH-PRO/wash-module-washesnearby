"""Unit tests for CRM credit payments + finance-stats → Owner finance mapping."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from main import (
    build_finance_payload,
    empty_bucket,
    local_day_bounds_utc,
    local_finance_date,
    money,
    parse_credit_payload,
    sum_buckets,
)


def credit(serial: str, amount: float, credit_type: int = 0) -> dict:
    return {
        "postSerial": serial,
        "messageType": "credit",
        "payload": {"type": credit_type, "summ": amount},
        "receivedAt": "2026-07-22T10:00:00Z",
    }


def stats_row(
    post_id: str,
    period: str,
    *,
    cash: float = 0,
    cashless: float = 0,
    discount: float = 0,
) -> dict:
    return {
        "postId": post_id,
        "period": period,
        "cash": cash,
        "cashless": cashless,
        "discountOps": discount,
        "recordedAt": "2026-07-22T12:00:00Z",
    }


class FinanceMappingTests(unittest.TestCase):
    def test_money_and_sum(self):
        self.assertEqual(money(-5), 0.0)
        self.assertEqual(money("12.345"), 12.35)
        a = {"cash": 10, "external": 20, "discount": 1}
        b = {"cash": 3, "external": 5, "discount": 1}
        self.assertEqual(sum_buckets([a, b])["cash"], 13.0)

    def test_local_finance_date_yekaterinburg(self):
        now = datetime(2026, 7, 21, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(local_finance_date("Asia/Yekaterinburg", now), "2026-07-22")

    def test_local_day_bounds_utc(self):
        start, end = local_day_bounds_utc("Asia/Yekaterinburg", "2026-07-22")
        self.assertTrue(start.startswith("2026-07-21T19:00:00"))
        self.assertTrue(end.startswith("2026-07-22T18:59:59"))

    def test_parse_credit_payload_types(self):
        self.assertEqual(
            parse_credit_payload(credit("S1", 100, 0)),
            {"cash": 100.0, "external": 0.0, "discount": 0.0},
        )
        self.assertEqual(
            parse_credit_payload(credit("S1", 200, 1)),
            {"cash": 0.0, "external": 200.0, "discount": 0.0},
        )
        self.assertEqual(
            parse_credit_payload(credit("S1", 30, 2)),
            {"cash": 0.0, "external": 0.0, "discount": 30.0},
        )
        self.assertIsNone(parse_credit_payload({"payload": {"type": 0, "summ": 0}}))

    def test_omits_when_no_posts(self):
        self.assertIsNone(
            build_finance_payload([], [credit("S1", 50)], finance_date="2026-07-22")
        )

    def test_today_from_credits_before_after_from_stats(self):
        posts = [
            {"id": "p1", "postNumber": 1, "serialNumber": "POST-1", "washId": "wash1"},
            {"id": "p2", "postNumber": 2, "serialNumber": "POST-2", "washId": "wash1"},
        ]
        rows = [
            credit("POST-1", 100, 0),
            credit("POST-1", 50, 1),
            credit("POST-2", 200, 1),
            credit("POST-2", 10, 2),
            credit("OTHER", 999, 0),  # other wash/post — ignored
        ]
        stats = [
            stats_row("p1", "before_collection", cash=1300, cashless=12550, discount=64),
            stats_row("p1", "after_collection", cash=0, cashless=0, discount=0),
            stats_row("p2", "before_collection", cash=200, cashless=300, discount=5),
            stats_row("p2", "after_collection", cash=10, cashless=20, discount=1),
        ]
        payload = build_finance_payload(posts, rows, stats, finance_date="2026-07-22")
        assert payload is not None
        self.assertEqual(payload["date"], "2026-07-22")
        self.assertEqual(
            payload["today"],
            {"cash": 100.0, "external": 250.0, "discount": 10.0},
        )
        self.assertEqual(
            payload["before_collection"],
            {"cash": 1500.0, "external": 12850.0, "discount": 69.0},
        )
        self.assertEqual(
            payload["after_collection"],
            {"cash": 10.0, "external": 20.0, "discount": 1.0},
        )
        self.assertEqual(payload["posts"][0]["today"]["cash"], 100.0)
        self.assertEqual(payload["posts"][0]["before_collection"]["cash"], 1300.0)
        self.assertEqual(payload["posts"][0]["before_collection"]["external"], 12550.0)
        self.assertEqual(payload["posts"][1]["today"]["external"], 200.0)
        self.assertEqual(payload["posts"][1]["before_collection"]["cash"], 200.0)

    def test_zero_today_when_no_credits_but_keeps_counters(self):
        posts = [{"id": "p1", "postNumber": 1, "serialNumber": "POST-1"}]
        stats = [
            stats_row("p1", "before_collection", cash=800, cashless=12000, discount=64),
            stats_row("p1", "after_collection"),
        ]
        payload = build_finance_payload(posts, [], stats, finance_date="2026-07-22")
        assert payload is not None
        self.assertEqual(payload["today"], empty_bucket())
        self.assertEqual(
            payload["before_collection"],
            {"cash": 800.0, "external": 12000.0, "discount": 64.0},
        )
        self.assertEqual(len(payload["posts"]), 1)

    def test_does_not_use_collection_counters_as_today(self):
        """Huge panel counters must never appear in today — only credit events."""
        posts = [{"id": "p1", "postNumber": 1, "serialNumber": "POST-1"}]
        junk = [
            {
                "postSerial": "POST-1",
                "period": "before_collection",
                "cash": 510,
                "cashless": 347335,
                "discountOps": 300,
            }
        ]
        payload = build_finance_payload(posts, junk, finance_date="2026-07-22")
        assert payload is not None
        self.assertEqual(payload["today"], empty_bucket())


if __name__ == "__main__":
    unittest.main()
