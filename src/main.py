"""WASH module: sync CRM washes → Автомойки рядом (Owner Integration API)."""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

MODULE_ID = "washesnearby"
LOG_PREFIX = "[washesnearby]"

API_BASE = os.environ.get("API_BASE_URL", "http://dynamic-api:3001").rstrip("/")
POST_ONLINE_SEC = 30
MAPPING_FILE = "wash_mapping.json"
SNAPSHOT_FILE = "last_snapshot.json"
SETTINGS_FILE = "settings.json"
SYNC_STATE_FILE = "sync_state.json"


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


def crm_get(path: str) -> Any:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def crm_list(path: str) -> list[dict]:
    data = crm_get(path)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


class MapsClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self.token:
            raise ValueError("OWNER_API_TOKEN не задан")
        url = f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} → HTTP {err.code}: {detail[:500]}") from err

        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {path}: unexpected response")
        if payload.get("success") is False:
            error = payload.get("error") or {}
            msg = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"{method} {path}: {msg or payload}")
        return payload.get("data", payload)

    def create_wash(self, body: dict) -> dict:
        data = self.request("POST", "/api/v1/integration/washes", body)
        if not isinstance(data, dict) or data.get("id") is None:
            raise RuntimeError("create wash: missing id in response")
        return data

    def patch_wash(self, remote_id: int | str, body: dict) -> Any:
        return self.request("PATCH", f"/api/v1/integration/washes/{remote_id}", body)

    def put_telemetry(self, remote_id: int | str, body: dict) -> Any:
        return self.request("PUT", f"/api/v1/integration/washes/{remote_id}/telemetry", body)


def load_mapping(settings: dict[str, Any]) -> dict[str, int]:
    """crmWashId → remote numeric id."""
    mapping: dict[str, int] = {}
    file_map = load_json(os.path.join(DATA_DIR, MAPPING_FILE), {})
    if isinstance(file_map, dict):
        for key, value in file_map.items():
            try:
                mapping[str(key)] = int(value)
            except (TypeError, ValueError):
                continue

    manual = parse_json_setting(pick_str(settings, "wash_mapping", "WASH_MAPPING", ""), {})
    if isinstance(manual, dict):
        for key, value in manual.items():
            try:
                mapping[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return mapping


def save_mapping(mapping: dict[str, int]) -> None:
    save_json(os.path.join(DATA_DIR, MAPPING_FILE), mapping)


def load_wash_coords(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = parse_json_setting(pick_str(settings, "wash_coords", "WASH_COORDS", ""), {})
    return raw if isinstance(raw, dict) else {}


def resolve_geo(
    wash: dict,
    settings: dict[str, Any],
    coords_map: dict[str, dict[str, Any]],
) -> tuple[float, float, str]:
    wid = ref_id(wash)
    override = coords_map.get(wid) or coords_map.get(str(wid)) or {}
    if not isinstance(override, dict):
        override = {}

    lat = override.get("lat", override.get("latitude"))
    lng = override.get("lng", override.get("lon", override.get("longitude")))
    city = str(override.get("city") or pick_str(settings, "default_city", "DEFAULT_CITY", "") or "")

    if lat is None:
        lat = pick_number(settings, "default_latitude", "DEFAULT_LATITUDE", 0)
    if lng is None:
        lng = pick_number(settings, "default_longitude", "DEFAULT_LONGITUDE", 0)

    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError) as err:
        raise ValueError(f"Некорректные координаты для мойки {wid}") from err

    if lat_f == 0 and lng_f == 0:
        raise ValueError(
            f"Задайте default_latitude/default_longitude или wash_coords для мойки {wid}"
        )
    return lat_f, lng_f, city


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
    settings: dict[str, Any],
    coords_map: dict[str, dict[str, Any]],
    posts_payload: list[dict],
    service_modes: list[dict],
) -> int:
    wid = ref_id(wash)
    if wid in mapping:
        return mapping[wid]

    lat, lng, city = resolve_geo(wash, settings, coords_map)
    wash_type = pick_str(settings, "wash_type", "WASH_TYPE", "self_service") or "self_service"
    body: dict[str, Any] = {
        "name": str(wash.get("name") or f"Мойка {wid}"),
        "address": str(wash.get("address") or "Адрес не указан"),
        "city": city or None,
        "latitude": lat,
        "longitude": lng,
        "type": wash_type if wash_type in ("self_service", "robot", "manual") else "self_service",
        "description": str(wash.get("description") or ""),
        "is_24h": True,
        "posts": posts_payload or [{"number": "1", "status": "free"}],
        "service_modes": service_modes,
    }
    # drop None city
    if not body.get("city"):
        body.pop("city", None)

    created = client.create_wash(body)
    remote_id = int(created.get("id"))
    mapping[wid] = remote_id
    save_mapping(mapping)
    log(f"created remote wash crm={wid} → maps={remote_id}")
    return remote_id


def run_cycle() -> dict:
    settings = load_settings()
    poll_interval = pick_int(settings, "poll_interval", "POLL_INTERVAL", 60, 60, 3600)
    news_limit = pick_int(settings, "news_limit", "NEWS_LIMIT", 5, 1, 50)
    token = pick_str(settings, "owner_api_token", "OWNER_API_TOKEN", "")
    maps_base = pick_str(
        settings,
        "maps_api_base",
        "MAPS_API_BASE",
        "https://мойка-про.рф",
    ) or "https://мойка-про.рф"
    sync_card = pick_str(settings, "sync_card", "SYNC_CARD", "1").lower() not in ("0", "false", "no")
    sync_prices = pick_str(settings, "sync_prices", "SYNC_PRICES", "1").lower() not in ("0", "false", "no")
    sync_news = pick_str(settings, "sync_news", "SYNC_NEWS", "1").lower() not in ("0", "false", "no")
    sync_telemetry = pick_str(settings, "sync_telemetry", "SYNC_TELEMETRY", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    only_wash = pick_str(settings, "wash_id", "WASH_ID", "")

    now = datetime.now(timezone.utc)
    client = MapsClient(maps_base, token)
    mapping = load_mapping(settings)
    coords_map = load_wash_coords(settings)
    sync_state = load_json(os.path.join(DATA_DIR, SYNC_STATE_FILE), {})
    if not isinstance(sync_state, dict):
        sync_state = {}

    washes = crm_list("/api/crm/washes?limit=200")
    posts_all = crm_list("/api/crm/posts?limit=500")
    states = crm_list("/api/crm/post-states?limit=500")
    work_mode_rows = crm_list("/api/crm/work-modes?limit=100")
    messages = crm_list("/api/crm/info-messages?limit=500")

    work_modes = {
        str(m.get("code")): str(m.get("name") or m.get("code"))
        for m in work_mode_rows
        if m.get("code") is not None
    }
    state_by_post = latest_states(states)

    if only_wash:
        washes = [w for w in washes if ref_id(w) == only_wash]

    results: list[dict] = []
    errors: list[dict] = []

    for wash in washes:
        wid = ref_id(wash)
        if not wid:
            continue
        try:
            wash_posts = [p for p in posts_all if ref_id(p.get("washId")) == wid]
            service_modes = build_service_modes(wash_posts, work_modes)
            posts_payload = build_posts_payload(wash_posts, state_by_post)
            news, promos = build_news_and_promos(messages, wid, now, news_limit)

            remote_id = ensure_remote_wash(
                client,
                wash,
                mapping,
                settings,
                coords_map,
                posts_payload,
                service_modes,
            )

            patch_body: dict[str, Any] = {}
            if sync_card:
                patch_body["name"] = str(wash.get("name") or f"Мойка {wid}")
                patch_body["address"] = str(wash.get("address") or "Адрес не указан")
                if wash.get("description"):
                    patch_body["description"] = str(wash.get("description"))
                try:
                    lat, lng, city = resolve_geo(wash, settings, coords_map)
                    patch_body["latitude"] = lat
                    patch_body["longitude"] = lng
                    if city:
                        patch_body["city"] = city
                except ValueError:
                    pass
            if sync_prices and service_modes:
                patch_body["service_modes"] = service_modes
            if sync_news:
                patch_body["news"] = news
                patch_body["promotions"] = promos

            wash_state = sync_state.get(wid) if isinstance(sync_state.get(wid), dict) else {}
            fp = content_fingerprint(
                {
                    "card": {k: patch_body[k] for k in ("name", "address", "description", "latitude", "longitude", "city") if k in patch_body},
                    "modes": patch_body.get("service_modes"),
                    "news": patch_body.get("news"),
                    "promotions": patch_body.get("promotions"),
                }
            )
            patched = False
            if patch_body and fp != wash_state.get("contentFp"):
                # Avoid putting service_modes into every patch if only news changed —
                # still full replace is required by API when present.
                client.patch_wash(remote_id, patch_body)
                wash_state["contentFp"] = fp
                wash_state["lastPatchAt"] = now.isoformat()
                patched = True
                log(f"patched maps={remote_id} crm={wid}")

            telemetry_result = None
            last_tel = parse_dt(wash_state.get("lastTelemetryAt"))
            can_tel = (not last_tel) or (now.timestamp() - last_tel.timestamp() >= 60)
            if sync_telemetry and posts_payload and can_tel:
                tel_body: dict[str, Any] = {
                    "status": "open",
                    "posts": posts_payload,
                }
                # Prices only via PATCH to avoid re-moderation every minute
                telemetry_result = client.put_telemetry(remote_id, tel_body)
                wash_state["lastTelemetryAt"] = now.isoformat()
                ignored = bool(
                    isinstance(telemetry_result, dict) and telemetry_result.get("ignored")
                )
                log(f"telemetry maps={remote_id} ignored={ignored}")

            sync_state[wid] = wash_state
            results.append(
                {
                    "crmWashId": wid,
                    "remoteWashId": remote_id,
                    "name": wash.get("name"),
                    "modes": len(service_modes),
                    "posts": len(posts_payload),
                    "news": len(news),
                    "promotions": len(promos),
                    "patched": patched,
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
        "crmWashCount": len(washes),
        "mappedCount": len(mapping),
        "syncedThisCycle": len(results),
        "mapping": mapping,
        "results": results,
        "recentErrors": errors[-8:],
    }
    save_json(os.path.join(DATA_DIR, SNAPSHOT_FILE), snapshot)
    return snapshot


def main() -> None:
    while True:
        settings = load_settings()
        poll_interval = pick_int(settings, "poll_interval", "POLL_INTERVAL", 60, 60, 3600)
        try:
            snap = run_cycle()
            log(
                f"synced={snap['syncedThisCycle']} mapped={snap['mappedCount']} "
                f"errors={len(snap.get('recentErrors') or [])}"
            )
        except urllib.error.URLError as err:
            log(f"CRM API error: {err}")
        except Exception as err:  # noqa: BLE001
            log(f"error: {err}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
