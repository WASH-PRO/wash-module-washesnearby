"""WASH module: sync CRM washes → Автомойки рядом (Owner Integration API)."""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]

MODULE_ID = "washesnearby"
LOG_PREFIX = "[washesnearby]"

API_BASE = os.environ.get("API_BASE_URL", "http://dynamic-api:3001").rstrip("/")
# Punycode form of https://мойка-про.рф — ASCII-safe default for HTTP clients.
DEFAULT_MAPS_API_BASE = "https://xn----7sb0aeimehj.xn--p1ai"
DEFAULT_FINANCE_TIMEZONE = "Asia/Yekaterinburg"
POST_ONLINE_SEC = 30
MAPPING_FILE = "wash_mapping.json"
SNAPSHOT_FILE = "last_snapshot.json"
SETTINGS_FILE = "settings.json"
SYNC_STATE_FILE = "sync_state.json"

_crm_access_token: str | None = None
_crm_token_expires_at = 0.0


def resolve_data_dir() -> str:
    for key in ("MODULE_DATA_DIR", "SECRET_MODULE_DATA_DIR"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return raw.rstrip("/")
    shared = f"/modules/installed/{MODULE_ID}/data"
    if os.path.isdir("/modules/installed") or os.path.isdir("/modules"):
        try:
            os.makedirs(shared, exist_ok=True)
            return shared
        except OSError:
            pass
    fallback = os.path.join(os.getcwd(), "data")
    os.makedirs(fallback, exist_ok=True)
    return fallback


DATA_DIR = resolve_data_dir()


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "activity.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except OSError:
        pass


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_settings() -> dict[str, Any]:
    data = load_json(os.path.join(DATA_DIR, SETTINGS_FILE), {})
    return data if isinstance(data, dict) else {}


def pick_str(settings: dict[str, Any], key: str, env_key: str, default: str = "") -> str:
    raw = settings.get(key)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    env_val = os.environ.get(env_key, default)
    return str(env_val).strip() if env_val is not None else default


def pick_number(settings: dict[str, Any], key: str, env_key: str, default: float) -> float:
    if key in settings and settings[key] is not None and settings[key] != "":
        try:
            return float(settings[key])
        except (TypeError, ValueError):
            pass
    env_val = os.environ.get(env_key)
    if env_val is not None and str(env_val).strip():
        try:
            return float(env_val)
        except ValueError:
            pass
    return default


def pick_int(settings: dict[str, Any], key: str, env_key: str, default: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(pick_number(settings, key, env_key, default))))


def parse_json_setting(raw: str, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def ref_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("id") or value.get("_id") or "")
    return str(value)


def slugify(text: str, fallback: str = "mode") -> str:
    raw = (text or "").strip().lower()
    raw = re.sub(r"[^\w\-]+", "-", raw, flags=re.UNICODE)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw[:64] or fallback


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    cleaned = re.sub(r"</p>", "\n\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return html.unescape(cleaned).strip()


def parse_dt(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def state_row_ts(state: dict) -> float:
    for key in ("lastMessageAt", "recordedAt", "createdAt", "updatedAt"):
        dt = parse_dt(state.get(key) if isinstance(state.get(key), str) else None)
        if dt:
            return dt.timestamp()
    return 0.0


def post_online(state: dict | None) -> bool:
    if not state:
        return False
    if state.get("connected") is False:
        return False
    ts = state_row_ts(state)
    return ts > 0 and time.time() - ts <= POST_ONLINE_SEC


def resolve_program_number(state: dict) -> int | None:
    mode = str(state.get("mode") or "").strip().lower()
    if mode == "idle":
        return None
    if mode.startswith("program_"):
        try:
            return int(mode.split("_", 1)[1])
        except (IndexError, ValueError):
            return None
    mode_number = state.get("modeNumber")
    if mode_number is not None:
        try:
            return int(mode_number)
        except (TypeError, ValueError):
            pass
    return None


def post_busy(state: dict | None) -> bool:
    if not state or not post_online(state):
        return False
    mode = str(state.get("mode") or "").strip().lower()
    if mode == "idle":
        return False
    if resolve_program_number(state) == 9:
        return False
    return True


def post_status(state: dict | None) -> str:
    if not state or not post_online(state):
        return "broken"
    return "busy" if post_busy(state) else "free"


def crm_headers(*, auth: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if auth:
        token = ensure_crm_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def ensure_crm_token() -> str:
    """JWT for authenticated CRM endpoints (finance-stats). Uses service account."""
    global _crm_access_token, _crm_token_expires_at

    preset = (
        os.environ.get("CRM_ACCESS_TOKEN")
        or os.environ.get("SECRET_CRM_ACCESS_TOKEN")
        or ""
    ).strip()
    if preset:
        return preset

    now = time.time()
    if _crm_access_token and now < _crm_token_expires_at:
        return _crm_access_token

    login = (
        os.environ.get("SERVICE_LOGIN")
        or os.environ.get("SECRET_SERVICE_LOGIN")
        or "service"
    ).strip()
    password = (
        os.environ.get("SERVICE_PASSWORD")
        or os.environ.get("SECRET_SERVICE_PASSWORD")
        or "ServiceInternal123!"
    )
    body = json.dumps({"login": login, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/api/auth/login",
        data=body,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    token = ""
    if isinstance(data, dict):
        token = str(data.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("CRM login: accessToken missing")
    _crm_access_token = token
    # Refresh a bit before typical 15m expiry.
    _crm_token_expires_at = now + 14 * 60
    return token


def crm_get(path: str, *, auth: bool = False) -> Any:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers=crm_headers(auth=auth))
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if auth and err.code == 401:
            # Force re-login once.
            global _crm_access_token, _crm_token_expires_at
            _crm_access_token = None
            _crm_token_expires_at = 0.0
            req = urllib.request.Request(url, headers=crm_headers(auth=True))
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        else:
            raise
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def crm_list(path: str, *, auth: bool = False) -> list[dict]:
    data = crm_get(path, auth=auth)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def crm_list_all(
    path: str, *, page_size: int = 100, max_pages: int = 50, auth: bool = False
) -> list[dict]:
    """Page through a CRM list endpoint until exhausted."""
    base = path.split("?", 1)[0]
    query = path.split("?", 1)[1] if "?" in path else ""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        params["page"] = [str(page)]
        params["limit"] = [str(page_size)]
        qs = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
        chunk = crm_list(f"{base}?{qs}", auth=auth)
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
    return rows


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


def latest_finance_by_post(
    stats: list[dict[str, Any]], id_of: Callable[[Any], str]
) -> dict[str, dict]:
    """Return map `{postId:period}` → latest finance-stats row."""
    by_key: dict[str, dict] = {}
    for row in stats:
        if not isinstance(row, dict):
            continue
        post_key = id_of(row.get("postId"))
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


def finance_row_local_date(row: dict[str, Any], tz_name: str) -> str | None:
    """Local calendar date of a finance-stats row, or None if no timestamp."""
    for key in ("recordedAt", "updatedAt", "createdAt"):
        raw = row.get(key)
        if not raw or not isinstance(raw, str):
            continue
        text = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return local_finance_date(tz_name, dt)
        except ValueError:
            continue
    return None


def build_finance_payload(
    wash_id: str,
    wash_posts: list[dict[str, Any]],
    finance_stats: list[dict[str, Any]],
    wash_state: dict[str, Any],
    *,
    finance_date: str,
    finance_timezone: str = DEFAULT_FINANCE_TIMEZONE,
    ref_id: Callable[[Any], str],
) -> dict[str, Any] | None:
    """
    Build Owner Integration `finance` block from CRM `/api/crm/finance-stats`.

    CRM has no day ledger — only live before/after counters (updated today).
    - Use only rows whose recordedAt falls on finance_date (local TZ).
    - today = after_collection − day_start, where day_start is fixed on first
      sighting of the calendar day as (after − before). That equals before_collection
      at first sync and then accumulates new revenue — no yesterday required.
    - cashless → external

    Returns None when this wash has no today's finance-stats rows.
    """
    wash_rows = []
    for row in finance_stats:
        if not isinstance(row, dict) or ref_id(row.get("washId")) != wash_id:
            continue
        row_date = finance_row_local_date(row, finance_timezone)
        if row_date is not None and row_date != finance_date:
            continue
        wash_rows.append(row)
    if not wash_rows:
        return None

    latest = latest_finance_by_post(wash_rows, ref_id)
    if not latest:
        return None

    baseline = wash_state.get("financeBaseline")
    if not isinstance(baseline, dict):
        baseline = {}
    baseline_date = str(baseline.get("date") or "")
    # dayStartByPost: lifetime counters at local day start (after − before on first sighting).
    day_start_by_post = baseline.get("dayStartByPost")
    if not isinstance(day_start_by_post, dict):
        day_start_by_post = {}

    new_day = baseline_date != finance_date
    next_day_start: dict[str, dict[str, float]] = {}
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

        stored = day_start_by_post.get(number) if not new_day else None
        if not isinstance(stored, dict):
            # First sighting today: day_start ≈ lifetime at last collection = after − before.
            # Then today ≈ before (all of current period counted as today).
            day_start = sub_buckets(after, before)
            today = sub_buckets(after, day_start)
            next_day_start[number] = dict(day_start)
        else:
            day_start = stored
            # Device wipe / counter reset: re-seed from today's live rows only.
            if (
                money(after.get("cash")) < money(day_start.get("cash"))
                or money(after.get("external")) < money(day_start.get("external"))
                or money(after.get("discount")) < money(day_start.get("discount"))
            ):
                day_start = sub_buckets(after, before)
                today = sub_buckets(after, day_start)
                next_day_start[number] = dict(day_start)
            else:
                today = sub_buckets(after, day_start)
                next_day_start[number] = dict(day_start)

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
        "dayStartByPost": next_day_start,
    }

    return {
        "date": finance_date,
        "today": sum_buckets([p["today"] for p in posts_out]),
        "before_collection": sum_buckets([p["before_collection"] for p in posts_out]),
        "after_collection": sum_buckets([p["after_collection"] for p in posts_out]),
        "posts": posts_out,
    }


def to_ascii_url(url: str) -> str:
    """Encode IDN host to punycode — urllib Host headers must be latin-1."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return url
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError:
        return url
    netloc = host_ascii
    if parts.port:
        netloc = f"{host_ascii}:{parts.port}"
    if parts.username is not None:
        user = parts.username
        if parts.password is not None:
            user = f"{user}:{parts.password}"
        netloc = f"{user}@{netloc}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _parse_json_response(method: str, path: str, status: int, raw: str) -> Any:
    compact = " ".join((raw or "").split())
    if status < 200 or status >= 300:
        raise RuntimeError(f"{method} {path} → HTTP {status}: {compact[:400]}")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as err:
        raise RuntimeError(f"{method} {path}: invalid JSON: {compact[:200]}") from err
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path}: unexpected response")
    if payload.get("success") is False:
        error = payload.get("error") or {}
        msg = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"{method} {path}: {msg or compact[:200]}")
    return payload.get("data", payload)


def maps_http_request(method: str, url: str, token: str, body: dict | None = None) -> Any:
    """
    Call Maps Owner API.

    Production host (мойка-про.рф) serves the API correctly only over HTTP/2
    (HTTP/1.1 hits a misconfigured Apache and returns 505). PyOrchestrator
    runtime includes curl with HTTP/2; urllib is HTTP/1.1-only and is a fallback.
    """
    ascii_url = to_ascii_url(url)
    path = urllib.parse.urlsplit(ascii_url).path or "/"
    payload_bytes = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    def via_curl(use_http2: bool) -> tuple[int, str]:
        curl_bin = shutil.which("curl")
        if not curl_bin:
            raise RuntimeError("curl not found")
        out_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                out_path = tmp.name
            cmd = [
                curl_bin,
                "-sS",
                "-o",
                out_path,
                "-w",
                "%{http_code}",
                "-X",
                method,
                "--max-time",
                "60",
            ]
            if use_http2:
                cmd.append("--http2")
            for key, value in headers.items():
                cmd.extend(["-H", f"{key}: {value}"])
            if payload_bytes is not None:
                cmd.extend(["--data-binary", "@-"])
            cmd.append(ascii_url)
            proc = subprocess.run(
                cmd,
                input=payload_bytes,
                capture_output=True,
                timeout=70,
                check=False,
            )
            status_text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            try:
                with open(out_path, encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
            except OSError:
                raw = ""
            if not status_text.isdigit():
                err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(err or f"curl exit {proc.returncode}")
            return int(status_text), raw
        finally:
            if out_path:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

    transport_errors: list[str] = []

    if shutil.which("curl"):
        for use_http2 in (True, False):
            try:
                status, raw = via_curl(use_http2)
            except Exception as err:  # noqa: BLE001
                transport_errors.append(str(err))
                err_text = str(err).lower()
                if use_http2 and ("http2" in err_text or "unsupported" in err_text):
                    continue
                break
            if status == 505:
                transport_errors.append(f"HTTP 505 (http2={use_http2})")
                continue
            return _parse_json_response(method, path, status, raw)

    req = urllib.request.Request(
        ascii_url,
        data=payload_bytes,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return _parse_json_response(method, path, getattr(resp, "status", 200) or 200, raw)
    except urllib.error.HTTPError as err:
        detail = " ".join(err.read().decode("utf-8", errors="replace").split())
        if err.code == 505:
            hint = "; ".join(transport_errors[:3])
            raise RuntimeError(
                f"{method} {path} → HTTP 505: сайт принимает Owner API только по HTTP/2. "
                f"Нужен curl с HTTP/2 в runtime PyOrchestrator."
                + (f" ({hint})" if hint else "")
                + f" Ответ: {detail[:200]}"
            ) from err
        raise RuntimeError(f"{method} {path} → HTTP {err.code}: {detail[:400]}") from err
    except Exception as err:
        if transport_errors:
            raise RuntimeError(
                f"{method} {path}: {err}; curl attempts: {'; '.join(transport_errors[:3])}"
            ) from err
        raise


class MapsClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = to_ascii_url(base_url.rstrip("/"))
        self.token = token.strip()

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self.token:
            raise ValueError("OWNER_API_TOKEN не задан")
        url = f"{self.base_url}{path}"
        return maps_http_request(method, url, self.token, body)

    def create_wash(self, body: dict) -> dict:
        data = self.request("POST", "/api/v1/integration/washes", body)
        if not isinstance(data, dict) or data.get("id") is None:
            raise RuntimeError("create wash: missing id in response")
        return data

    def find_by_external_id(self, external_id: str) -> dict | None:
        query = urllib.parse.urlencode({"external_id": external_id, "per_page": 5})
        data = self.request("GET", f"/api/v1/integration/washes?{query}")
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and str(row.get("external_id") or "") == str(external_id):
                    return row
            if data and isinstance(data[0], dict):
                return data[0]
        return None

    def patch_wash(self, wash_ref: int | str, body: dict) -> Any:
        return self.request("PATCH", f"/api/v1/integration/washes/{wash_ref}", body)

    def put_telemetry(self, wash_ref: int | str, body: dict) -> Any:
        return self.request("PUT", f"/api/v1/integration/washes/{wash_ref}/telemetry", body)


def maps_wash_ref(external_id: str) -> str:
    """Prefer ext:{uuid} so API paths stay stable."""
    return f"ext:{external_id}"


def wash_maps_uuid(wash: dict) -> str:
    """CRM mapsExternalId (UUID) used as Owner API external_id."""
    return str(wash.get("mapsExternalId") or wash.get("maps_external_id") or "").strip()


def load_mapping() -> dict[str, int]:
    """crmWashId → remote numeric id (cache only; identity is mapsExternalId UUID)."""
    mapping: dict[str, int] = {}
    file_map = load_json(os.path.join(DATA_DIR, MAPPING_FILE), {})
    if isinstance(file_map, dict):
        for key, value in file_map.items():
            try:
                mapping[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return mapping


def save_mapping(mapping: dict[str, int]) -> None:
    save_json(os.path.join(DATA_DIR, MAPPING_FILE), mapping)


def _as_settings_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = parse_json_setting(raw, {})
        return parsed if isinstance(parsed, dict) else {}
    return {}


def load_washes_config(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Per-wash params from settings.washes (preferred).

    Migrates legacy wash_coords / defaults / wash_type when washes is empty.
    """
    washes = _as_settings_dict(settings.get("washes"))
    if not washes:
        washes = _as_settings_dict(
            settings.get("wash_coords")
            or pick_str(settings, "wash_coords", "WASH_COORDS", "")
            or pick_str(settings, "washes", "WASHES", "")
        )

    default_lat = settings.get("default_latitude")
    default_lng = settings.get("default_longitude")
    default_city = str(settings.get("default_city") or "").strip()
    default_type = pick_str(settings, "wash_type", "WASH_TYPE", "self_service") or "self_service"

    result: dict[str, dict[str, Any]] = {}
    for key, value in washes.items():
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        # Legacy wash_coords used lat/lng
        if "latitude" not in entry and "lat" in entry:
            entry["latitude"] = entry.get("lat")
        if "longitude" not in entry and ("lng" in entry or "lon" in entry):
            entry["longitude"] = entry.get("lng", entry.get("lon"))
        if "enabled" not in entry:
            entry["enabled"] = True
        if not entry.get("type"):
            entry["type"] = default_type
        result[str(key)] = entry

    # If only global defaults existed (no per-wash map), keep them available
    # via a special key used only when a wash has no own entry.
    if default_lat is not None or default_lng is not None or default_city:
        result.setdefault(
            "__defaults__",
            {
                "enabled": True,
                "latitude": default_lat,
                "longitude": default_lng,
                "city": default_city,
                "type": default_type,
            },
        )
    return result


def wash_entry(washes_cfg: dict[str, dict[str, Any]], wid: str) -> dict[str, Any]:
    entry = washes_cfg.get(wid) or washes_cfg.get(str(wid))
    if isinstance(entry, dict):
        return entry
    defaults = washes_cfg.get("__defaults__")
    return defaults if isinstance(defaults, dict) else {}


def wash_enabled(entry: dict[str, Any]) -> bool:
    if "enabled" not in entry:
        return True
    raw = entry.get("enabled")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def resolve_geo(wid: str, entry: dict[str, Any]) -> tuple[float, float, str]:
    lat = entry.get("latitude", entry.get("lat"))
    lng = entry.get("longitude", entry.get("lng", entry.get("lon")))
    city = str(entry.get("city") or "").strip()

    try:
        lat_f = float(lat) if lat is not None and lat != "" else 0.0
        lng_f = float(lng) if lng is not None and lng != "" else 0.0
    except (TypeError, ValueError) as err:
        raise ValueError(f"Некорректные координаты для мойки {wid}") from err

    if lat_f == 0 and lng_f == 0:
        raise ValueError(
            f"Задайте широту и долготу для мойки {wid} в настройках модуля"
        )
    return lat_f, lng_f, city


def resolve_wash_type(entry: dict[str, Any]) -> str:
    wash_type = str(entry.get("type") or "self_service").strip() or "self_service"
    return wash_type if wash_type in ("self_service", "robot", "manual") else "self_service"


def latest_states(states: list[dict]) -> dict[str, dict]:
    by_post: dict[str, dict] = {}
    for row in states:
        pid = ref_id(row.get("postId"))
        if not pid:
            continue
        prev = by_post.get(pid)
        if not prev or state_row_ts(row) >= state_row_ts(prev):
            by_post[pid] = row
    return by_post


def build_service_modes(posts: list[dict], work_modes: dict[str, str]) -> list[dict]:
    """Max price across all posts for each mode code; names from work-modes catalog."""
    max_prices: dict[str, int] = {}
    for post in posts:
        settings = post.get("settings") if isinstance(post.get("settings"), dict) else {}
        prices = settings.get("modePrices") if isinstance(settings.get("modePrices"), dict) else {}
        for code, price in prices.items():
            try:
                value = int(round(float(price)))
            except (TypeError, ValueError):
                continue
            if value < 0:
                continue
            key = str(code)
            if key not in max_prices or value > max_prices[key]:
                max_prices[key] = value

    modes: list[dict] = []
    for code in sorted(max_prices.keys(), key=lambda k: int(k) if str(k).isdigit() else 999):
        name = work_modes.get(str(code)) or f"Режим {code}"
        modes.append(
            {
                "slug": slugify(f"mode-{code}", fallback=f"mode-{code}"),
                "name": name,
                "price": max_prices[code],
                "sort_order": int(code) if str(code).isdigit() else 999,
            }
        )
    return modes


def build_posts_payload(posts: list[dict], state_by_post: dict[str, dict]) -> list[dict]:
    result: list[dict] = []
    ordered = sorted(posts, key=lambda p: int(p.get("postNumber") or 0))
    for post in ordered:
        number = str(post.get("postNumber") if post.get("postNumber") is not None else post.get("name") or "")
        if not number:
            continue
        state = state_by_post.get(ref_id(post))
        result.append(
            {
                "number": number,
                "status": post_status(state),
                "label": str(post.get("name") or f"Пост {number}"),
            }
        )
    return result


def count_post_statuses(posts_payload: list[dict]) -> dict[str, int]:
    counts = {"free": 0, "busy": 0, "broken": 0}
    for row in posts_payload:
        status = str(row.get("status") or "broken")
        if status not in counts:
            status = "broken"
        counts[status] += 1
    return counts


def message_active(message: dict, now: datetime) -> bool:
    status = str(message.get("status") or "").strip().lower()
    if status not in ("scheduled", "published"):
        return False
    published_at = parse_dt(message.get("publishedAt"))
    if not published_at or published_at > now:
        return False
    expires_at = parse_dt(message.get("expiresAt"))
    if expires_at and expires_at <= now:
        return False
    return True


def build_news_and_promos(
    messages: list[dict],
    wash_id: str,
    now: datetime,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    news: list[dict] = []
    promos: list[dict] = []

    relevant = []
    for msg in messages:
        if not message_active(msg, now):
            continue
        msg_wash = ref_id(msg.get("washId"))
        if msg_wash and msg_wash != wash_id:
            continue
        relevant.append(msg)

    relevant.sort(
        key=lambda m: parse_dt(m.get("publishedAt")) or datetime.fromtimestamp(0, tz=timezone.utc),
        reverse=True,
    )

    for msg in relevant:
        category = str(msg.get("category") or "general").strip().lower()
        title = str(msg.get("title") or "").strip()
        body = strip_html(str(msg.get("body") or ""))
        if not title:
            continue
        if category == "promotion":
            if len(promos) >= limit:
                continue
            item: dict[str, Any] = {"title": title, "description": body}
            ends = parse_dt(msg.get("expiresAt"))
            if ends:
                item["ends_at"] = ends.isoformat()
            # optional discount from title like "Скидка 20%"
            m = re.search(r"(\d+)\s*%", title)
            if m:
                item["discount_percent"] = int(m.group(1))
            promos.append(item)
        elif category in ("news", "general"):
            if len(news) >= limit:
                continue
            published = parse_dt(msg.get("publishedAt"))
            news.append(
                {
                    "title": title,
                    "content": body or title,
                    "published_at": published.isoformat() if published else None,
                }
            )
    return news, promos


def content_fingerprint(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_remote_wash(
    client: MapsClient,
    wash: dict,
    mapping: dict[str, int],
    wash_cfg: dict[str, Any],
    posts_payload: list[dict],
    service_modes: list[dict],
    wash_state: dict[str, Any],
) -> tuple[int, str, str]:
    """
    Return (platformId, apiRef, externalUuid).

    Identity on the maps site is CRM mapsExternalId (UUID) → Owner API external_id.
    """
    wid = ref_id(wash)
    external_uuid = wash_maps_uuid(wash)
    if not external_uuid:
        raise ValueError(
            f"У мойки CRM {wid} нет mapsExternalId (UUID). "
            "Обновите CRM / выполните init-seed или откройте мойку в Dashboard и сохраните."
        )
    try:
        uuid.UUID(external_uuid)
    except ValueError as err:
        raise ValueError(f"Некорректный mapsExternalId у мойки CRM {wid}: {external_uuid}") from err

    ext_ref = maps_wash_ref(external_uuid)

    existing = None
    try:
        existing = client.find_by_external_id(external_uuid)
    except Exception as err:  # noqa: BLE001
        log(f"lookup by external_id uuid={external_uuid}: {err}")

    if existing and existing.get("id") is not None:
        remote_id = int(existing["id"])
        mapping[wid] = remote_id
        save_mapping(mapping)
        wash_state["externalIdSet"] = True
        wash_state["mapsExternalId"] = external_uuid
        return remote_id, ext_ref, external_uuid

    if wid in mapping:
        remote_id = mapping[wid]
        if not wash_state.get("externalIdSet") or wash_state.get("mapsExternalId") != external_uuid:
            try:
                client.patch_wash(remote_id, {"external_id": external_uuid})
                wash_state["externalIdSet"] = True
                wash_state["mapsExternalId"] = external_uuid
                log(f"set external_id uuid={external_uuid} on maps={remote_id} crm={wid}")
                return remote_id, ext_ref, external_uuid
            except Exception as err:  # noqa: BLE001
                log(f"set external_id failed crm={wid} maps={remote_id}: {err}")
                return remote_id, str(remote_id), external_uuid
        return remote_id, ext_ref, external_uuid

    lat, lng, city = resolve_geo(wid, wash_cfg)
    body: dict[str, Any] = {
        "name": str(wash.get("name") or f"Мойка {wid}"),
        "external_id": external_uuid,
        "address": str(wash.get("address") or "Адрес не указан"),
        "city": city or None,
        "latitude": lat,
        "longitude": lng,
        "type": resolve_wash_type(wash_cfg),
        "description": str(wash.get("description") or ""),
        "is_24h": True,
        "posts": posts_payload or [{"number": "1", "status": "free"}],
        "service_modes": service_modes,
    }
    if not body.get("city"):
        body.pop("city", None)

    created = client.create_wash(body)
    remote_id = int(created.get("id"))
    mapping[wid] = remote_id
    save_mapping(mapping)
    wash_state["externalIdSet"] = True
    wash_state["mapsExternalId"] = external_uuid
    log(f"created remote wash crm={wid} → maps={remote_id} external_id={external_uuid}")
    return remote_id, ext_ref, external_uuid


def run_cycle() -> dict:
    settings = load_settings()
    # Site marks wash offline if telemetry is older than ~3 minutes.
    poll_interval = pick_int(settings, "poll_interval", "POLL_INTERVAL", 60, 60, 120)
    news_limit = pick_int(settings, "news_limit", "NEWS_LIMIT", 5, 1, 50)
    token = pick_str(settings, "owner_api_token", "OWNER_API_TOKEN", "")
    maps_base = pick_str(
        settings,
        "maps_api_base",
        "MAPS_API_BASE",
        DEFAULT_MAPS_API_BASE,
    ) or DEFAULT_MAPS_API_BASE
    sync_card = pick_str(settings, "sync_card", "SYNC_CARD", "1").lower() not in ("0", "false", "no")
    sync_prices = pick_str(settings, "sync_prices", "SYNC_PRICES", "1").lower() not in ("0", "false", "no")
    sync_news = pick_str(settings, "sync_news", "SYNC_NEWS", "1").lower() not in ("0", "false", "no")
    sync_telemetry = pick_str(settings, "sync_telemetry", "SYNC_TELEMETRY", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    sync_finance = pick_str(settings, "sync_finance", "SYNC_FINANCE", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    finance_tz = (
        pick_str(settings, "finance_timezone", "FINANCE_TIMEZONE", DEFAULT_FINANCE_TIMEZONE)
        or DEFAULT_FINANCE_TIMEZONE
    )
    now = datetime.now(timezone.utc)
    finance_date = local_finance_date(finance_tz, now)
    client = MapsClient(maps_base, token)
    mapping = load_mapping()
    washes_cfg = load_washes_config(settings)
    sync_state = load_json(os.path.join(DATA_DIR, SYNC_STATE_FILE), {})
    if not isinstance(sync_state, dict):
        sync_state = {}

    washes = crm_list("/api/crm/washes?limit=200")
    posts_all = crm_list("/api/crm/posts?limit=500")
    states = crm_list("/api/crm/post-states?limit=500")
    work_mode_rows = crm_list("/api/crm/work-modes?limit=100")
    messages = crm_list("/api/crm/info-messages?limit=500")
    finance_stats: list[dict] = []
    if sync_finance:
        try:
            finance_stats = crm_list_all("/api/crm/finance-stats", auth=True)
        except Exception as err:  # noqa: BLE001
            log(f"finance-stats unavailable (auth/API): {err}")
            finance_stats = []

    work_modes = {
        str(m.get("code")): str(m.get("name") or m.get("code"))
        for m in work_mode_rows
        if m.get("code") is not None
    }
    state_by_post = latest_states(states)

    results: list[dict] = []
    errors: list[dict] = []
    totals = {"free": 0, "busy": 0, "broken": 0, "posts": 0}
    skipped = 0

    for wash in washes:
        wid = ref_id(wash)
        if not wid:
            continue
        entry = wash_entry(washes_cfg, wid)
        if not wash_enabled(entry):
            skipped += 1
            continue
        try:
            wash_posts = [p for p in posts_all if ref_id(p.get("washId")) == wid]
            service_modes = build_service_modes(wash_posts, work_modes)
            posts_payload = build_posts_payload(wash_posts, state_by_post)
            status_counts = count_post_statuses(posts_payload)
            news, promos = build_news_and_promos(messages, wid, now, news_limit)

            wash_state = sync_state.get(wid) if isinstance(sync_state.get(wid), dict) else {}
            remote_id, wash_ref, external_uuid = ensure_remote_wash(
                client,
                wash,
                mapping,
                entry,
                posts_payload,
                service_modes,
                wash_state,
            )

            patch_body: dict[str, Any] = {}
            if sync_card:
                patch_body["name"] = str(wash.get("name") or f"Мойка {wid}")
                patch_body["address"] = str(wash.get("address") or "Адрес не указан")
                if wash.get("description"):
                    patch_body["description"] = str(wash.get("description"))
                patch_body["type"] = resolve_wash_type(entry)
                try:
                    lat, lng, city = resolve_geo(wid, entry)
                    patch_body["latitude"] = lat
                    patch_body["longitude"] = lng
                    if city:
                        patch_body["city"] = city
                except ValueError:
                    pass
            if sync_prices and service_modes:
                patch_body["service_modes"] = service_modes
            if sync_news:
                # Avoid null published_at — some API validators reject null fields.
                patch_body["news"] = [
                    {k: v for k, v in item.items() if v is not None} for item in news
                ]
                patch_body["promotions"] = [
                    {k: v for k, v in item.items() if v is not None} for item in promos
                ]

            fp = content_fingerprint(
                {
                    "card": {
                        k: patch_body[k]
                        for k in (
                            "name",
                            "address",
                            "description",
                            "latitude",
                            "longitude",
                            "city",
                            "type",
                        )
                        if k in patch_body
                    },
                    "modes": patch_body.get("service_modes"),
                    "news": patch_body.get("news"),
                    "promotions": patch_body.get("promotions"),
                }
            )
            patched = False
            if patch_body and fp != wash_state.get("contentFp"):
                try:
                    client.patch_wash(wash_ref, patch_body)
                    wash_state["contentFp"] = fp
                    wash_state["lastPatchAt"] = now.isoformat()
                    patched = True
                    log(f"patched maps={remote_id} ref={wash_ref} crm={wid}")
                except Exception as patch_err:  # noqa: BLE001
                    err_text = str(patch_err)
                    errors.append({"crmWashId": wid, "error": f"patch: {err_text}", "name": wash.get("name")})
                    log(f"patch error crm={wid}: {err_text}")

            telemetry_result = None
            finance_summary = None
            last_tel = parse_dt(wash_state.get("lastTelemetryAt"))
            can_tel = (not last_tel) or (now.timestamp() - last_tel.timestamp() >= 55)
            if sync_telemetry and posts_payload and can_tel:
                tel_body: dict[str, Any] = {
                    "status": "open",
                    "posts": posts_payload,
                }
                finance = None
                if sync_finance:
                    finance = build_finance_payload(
                        wid,
                        wash_posts,
                        finance_stats,
                        wash_state,
                        finance_date=finance_date,
                        finance_timezone=finance_tz,
                        ref_id=ref_id,
                    )
                    if finance:
                        tel_body["finance"] = finance
                        finance_summary = {
                            "date": finance.get("date"),
                            "today": finance.get("today"),
                            "before_collection": finance.get("before_collection"),
                            "after_collection": finance.get("after_collection"),
                            "posts": len(finance.get("posts") or []),
                        }
                try:
                    # Prices only via PATCH to avoid re-moderation every minute
                    telemetry_result = client.put_telemetry(wash_ref, tel_body)
                    ignored = bool(
                        isinstance(telemetry_result, dict) and telemetry_result.get("ignored")
                    )
                    if not ignored:
                        wash_state["lastTelemetryAt"] = now.isoformat()
                    load = (
                        telemetry_result.get("load")
                        if isinstance(telemetry_result, dict)
                        else None
                    )
                    today = (finance or {}).get("today") or {}
                    log(
                        f"telemetry maps={remote_id} ref={wash_ref} ignored={ignored} "
                        f"busy={status_counts['busy']} free={status_counts['free']} "
                        f"broken={status_counts['broken']} load={load}"
                        + (
                            f" finance_date={finance.get('date')} "
                            f"today_cash={today.get('cash', 0)} "
                            f"today_external={today.get('external', 0)} "
                            f"today_discount={today.get('discount', 0)} "
                            f"finance_posts={len(finance.get('posts') or [])}"
                            if finance
                            else ""
                        )
                    )
                except Exception as tel_err:  # noqa: BLE001
                    err_text = str(tel_err)
                    errors.append({"crmWashId": wid, "error": f"telemetry: {err_text}", "name": wash.get("name")})
                    log(f"telemetry error crm={wid}: {err_text}")

            sync_state[wid] = wash_state
            for key in ("free", "busy", "broken"):
                totals[key] += status_counts[key]
            totals["posts"] += len(posts_payload)
            results.append(
                {
                    "crmWashId": wid,
                    "remoteWashId": remote_id,
                    "externalId": external_uuid,
                    "mapsRef": wash_ref,
                    "name": wash.get("name"),
                    "modes": len(service_modes),
                    "posts": len(posts_payload),
                    "busy": status_counts["busy"],
                    "free": status_counts["free"],
                    "broken": status_counts["broken"],
                    "news": len(news),
                    "promotions": len(promos),
                    "patched": patched,
                    "finance": finance_summary,
                    "telemetry": telemetry_result,
                }
            )
        except Exception as err:  # noqa: BLE001
            err_text = str(err)
            errors.append({"crmWashId": wid, "error": err_text, "name": wash.get("name")})
            log(f"error crm={wid}: {err_text}")

    save_mapping(mapping)
    save_json(os.path.join(DATA_DIR, SYNC_STATE_FILE), sync_state)

    snapshot = {
        "recordedAt": now.isoformat(),
        "mapsApiBase": maps_base,
        "configured": bool(token),
        "pollInterval": poll_interval,
        "financeDate": finance_date,
        "financeTimezone": finance_tz,
        "crmWashCount": len(washes),
        "skippedCount": skipped,
        "mappedCount": len(mapping),
        "syncedThisCycle": len(results),
        "busy": totals["busy"],
        "free": totals["free"],
        "broken": totals["broken"],
        "posts": totals["posts"],
        "mapping": mapping,
        "results": results,
        "recentErrors": errors[-8:],
    }
    save_json(os.path.join(DATA_DIR, SNAPSHOT_FILE), snapshot)
    return snapshot


def main() -> None:
    while True:
        settings = load_settings()
        poll_interval = pick_int(settings, "poll_interval", "POLL_INTERVAL", 60, 60, 120)
        try:
            snap = run_cycle()
            log(
                f"synced={snap['syncedThisCycle']} mapped={snap['mappedCount']} "
                f"busy={snap.get('busy', 0)} free={snap.get('free', 0)} "
                f"broken={snap.get('broken', 0)} errors={len(snap.get('recentErrors') or [])}"
            )
        except urllib.error.URLError as err:
            log(f"CRM API error: {err}")
        except Exception as err:  # noqa: BLE001
            log(f"error: {err}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
