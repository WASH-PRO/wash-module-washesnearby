"""CRM finance-stats → Owner Integration telemetry `finance` block."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

DEFAULT_FINANCE_TIMEZONE = "Asia/Yekaterinburg"


def money(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        number = 0.0
    return round(number, 2)


def empty_bucket() -> dict[str, float]:
    return {"cash": 0.0, "external": 0.0, "discount": 0.0}


def bucket_from_crm_row(row: dict[str, Any] | None) -> dict[str, float]:
    if not row:
        return empty_bucket()
    external = row.get("cashless")
    if external is None:
        external = row.get("external")
    if external is None:
        external = row.get("card")
    discount = row.get("discountOps")
    if discount is None:
        discount = row.get("discount")
    return {
        "cash": money(row.get("cash")),
        "external": money(external),
        "discount": money(discount),
    }


def sum_buckets(buckets: list[dict[str, float]]) -> dict[str, float]:
    total = empty_bucket()
    for bucket in buckets:
        total["cash"] = money(total["cash"] + money(bucket.get("cash")))
        total["external"] = money(total["external"] + money(bucket.get("external")))
        total["discount"] = money(total["discount"] + money(bucket.get("discount")))
    return total


def sub_buckets(current: dict[str, float], baseline: dict[str, float]) -> dict[str, float]:
    """Delta current − baseline, floored at 0 (handles counter resets)."""
    return {
        "cash": money(max(0.0, money(current.get("cash")) - money(baseline.get("cash")))),
        "external": money(
            max(0.0, money(current.get("external")) - money(baseline.get("external")))
        ),
        "discount": money(
            max(0.0, money(current.get("discount")) - money(baseline.get("discount")))
        ),
    }


def finance_row_ts(row: dict[str, Any]) -> float:
    for key in ("recordedAt", "updatedAt", "createdAt"):
        raw = row.get(key)
        if not raw or not isinstance(raw, str):
            continue
        text = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def latest_finance_by_post(stats: list[dict[str, Any]], ref_id: Callable[[Any], str]) -> dict[str, dict]:
    """Return map `{postId:period}` → latest finance-stats row."""
    by_key: dict[str, dict] = {}
    for row in stats:
        if not isinstance(row, dict):
            continue
        post_key = ref_id(row.get("postId"))
        if not post_key:
            continue
        period = str(row.get("period") or "before_collection").strip() or "before_collection"
        key = f"{post_key}:{period}"
        prev = by_key.get(key)
        if not prev or finance_row_ts(row) >= finance_row_ts(prev):
            by_key[key] = row
    return by_key


def local_finance_date(tz_name: str, now: datetime | None = None) -> str:
    """Calendar YYYY-MM-DD in wash/CRM working timezone (not UTC-blind)."""
    now = now or datetime.now(timezone.utc)
    name = (tz_name or DEFAULT_FINANCE_TIMEZONE).strip() or DEFAULT_FINANCE_TIMEZONE
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(name)
            return now.astimezone(tz).date().isoformat()
        except Exception:  # noqa: BLE001
            pass
    return now.astimezone(timezone.utc).date().isoformat()


def build_finance_payload(
    wash_id: str,
    wash_posts: list[dict[str, Any]],
    finance_stats: list[dict[str, Any]],
    wash_state: dict[str, Any],
    *,
    finance_date: str,
    ref_id: Callable[[Any], str],
) -> dict[str, Any] | None:
    """
    Build Owner Integration `finance` block from CRM `/api/crm/finance-stats`.

    - before_collection / after_collection: live MQTT counters (CRM cash/cashless/discountOps)
    - today: day delta of after_collection vs baseline stored in wash_state
    - cashless → external

    Returns None when this wash has no finance-stats rows (omit block — do not zero-wipe).
    """
    wash_rows = [
        row
        for row in finance_stats
        if isinstance(row, dict) and ref_id(row.get("washId")) == wash_id
    ]
    if not wash_rows:
        return None

    latest = latest_finance_by_post(wash_rows, ref_id)
    if not latest:
        return None

    baseline = wash_state.get("financeBaseline")
    if not isinstance(baseline, dict):
        baseline = {}
    baseline_date = str(baseline.get("date") or "")
    after_by_post = baseline.get("afterByPost")
    if not isinstance(after_by_post, dict):
        after_by_post = {}

    new_day = baseline_date != finance_date
    next_after_by_post: dict[str, dict[str, float]] = {}
    posts_out: list[dict[str, Any]] = []

    ordered = sorted(wash_posts, key=lambda p: int(p.get("postNumber") or 0))
    for post in ordered:
        pid = ref_id(post)
        if not pid:
            continue
        number = str(
            post.get("postNumber") if post.get("postNumber") is not None else post.get("name") or ""
        )
        if not number:
            continue

        before_row = latest.get(f"{pid}:before_collection")
        after_row = latest.get(f"{pid}:after_collection")
        if not before_row and not after_row:
            continue

        before = bucket_from_crm_row(before_row)
        after = bucket_from_crm_row(after_row)

        if new_day:
            today = empty_bucket()
            next_after_by_post[number] = dict(after)
        else:
            prev = after_by_post.get(number)
            if not isinstance(prev, dict):
                prev = empty_bucket()
                next_after_by_post[number] = dict(after)
                today = empty_bucket()
            else:
                # Counter reset (device wipe / re-pair): re-baseline.
                if (
                    money(after.get("cash")) < money(prev.get("cash"))
                    or money(after.get("external")) < money(prev.get("external"))
                    or money(after.get("discount")) < money(prev.get("discount"))
                ):
                    today = empty_bucket()
                    next_after_by_post[number] = dict(after)
                else:
                    today = sub_buckets(after, prev)
                    next_after_by_post[number] = dict(prev)

        posts_out.append(
            {
                "number": number,
                "today": today,
                "before_collection": before,
                "after_collection": after,
            }
        )

    if not posts_out:
        return None

    wash_state["financeBaseline"] = {
        "date": finance_date,
        "afterByPost": next_after_by_post,
    }

    return {
        "date": finance_date,
        "today": sum_buckets([p["today"] for p in posts_out]),
        "before_collection": sum_buckets([p["before_collection"] for p in posts_out]),
        "after_collection": sum_buckets([p["after_collection"] for p in posts_out]),
        "posts": posts_out,
    }
