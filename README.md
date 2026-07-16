**Language:** English (default) · [Русский](README.ru.md)

# wash-module-washesnearby

WASH PRO CRM module: sync car washes to **Washes Nearby** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## What it does

1. Creates a wash on the map site (name + address + coordinates) if there is no mapping yet
2. Syncs **service modes / prices** — for each work mode, takes the **maximum** price across all posts of that wash (mode names come from CRM work-modes catalog; names are the same across posts)
3. Syncs **post occupancy** via telemetry (`free` / `busy` / `broken`)
4. Syncs latest **news** and **promotions** from CRM Publications (`info-messages`)

## Settings

| Key | Description |
|-----|-------------|
| `owner_api_token` | Owner API Bearer token (Owner cabinet → API) |
| `maps_api_base` | API base URL (default punycode `https://xn----7sb0aeimehj.xn--p1ai` = мойка-про.рф). Owner API works over HTTP/2 — the module calls it via `curl --http2`. |
| `default_latitude` / `default_longitude` / `default_city` | Used when creating a wash (CRM has address text only) |
| `wash_coords` | Optional JSON per CRM wash: `{"crmId":{"lat":55.16,"lng":61.4,"city":"…"}}` |
| `wash_mapping` | Optional pre-link: `{"crmId": 12}` → site wash id `12` |
| `wash_id` | Sync only one CRM wash (empty = all) |
| `poll_interval` | Seconds (60–120). Site marks wash offline without telemetry after ~3 min; API accepts telemetry ≤ 1/min. |
| `news_limit` | Max news / promotions per wash |

## Mapping CRM wash ↔ site wash

The Integration API links washes via **`external_id` = CRM wash id**. The module:

1. Sends `external_id` on create
2. Looks up existing washes with `?external_id=`
3. Patches older washes that are missing `external_id`
4. Calls patch/telemetry as `ext:{crmWashId}`

You can still set `wash_mapping` manually (`{"crmId": 12}`) if the wash already exists on the site without a CRM id.

## Install

Dashboard → Automation → Modules → **Washes Nearby** → Install → Settings → Start.

Requires PyOrchestrator (`PYORCHESTRATOR_ENABLED=true`).

## Data files

| File | Purpose |
|------|---------|
| `data/wash_mapping.json` | CRM wash id → site wash id |
| `data/sync_state.json` | Content fingerprints + last telemetry time |
| `data/last_snapshot.json` | UI overview snapshot |
| `data/settings.json` | Module settings |
