**Language:** English (default) · [Русский](README.ru.md)

# wash-module-washesnearby

WASH PRO CRM module: sync car washes to **Washes Nearby** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## What it does

1. Creates a wash on the map site (name + address + coordinates) if there is no mapping yet
2. Syncs **service modes / prices** — for each work mode, takes the **maximum** price across all posts of that wash (mode names come from CRM work-modes catalog; names are the same across posts)
3. Syncs **post occupancy** via telemetry (`free` / `busy` / `broken`)
4. Syncs latest **news** and **promotions** from CRM Publications (`info-messages`)

## CRM UUID ↔ site `external_id`

Each CRM wash has **`mapsExternalId`** (UUID v4). The module sends it to the site as Owner API **`external_id`**:

| Step | What happens |
|------|----------------|
| Create wash in CRM | Dashboard assigns `mapsExternalId` |
| Existing washes | `init-seed` backfills missing UUIDs |
| Module sync | Looks up / creates / patches site wash by that UUID |
| API calls | `PATCH` / telemetry use `ext:{uuid}` |

CRM does **not** need the site’s numeric wash id. The module may keep `data/wash_mapping.json` as a cache only.

If a wash has no `mapsExternalId`, the module skips it with an error — update CRM / run `init-seed`, or open the wash in Dashboard and save.

## Settings

| Key | Description |
|-----|-------------|
| `owner_api_token` | Owner API Bearer token (Owner cabinet → API) |
| `maps_api_base` | API base URL (default punycode `https://xn----7sb0aeimehj.xn--p1ai` = мойка-про.рф). Owner API works over HTTP/2 — the module calls it via `curl --http2`. |
| `default_latitude` / `default_longitude` / `default_city` | Used when creating a wash (CRM has address text only) |
| `wash_coords` | Optional JSON per CRM wash: `{"crmId":{"lat":55.16,"lng":61.4,"city":"…"}}` |
| `wash_mapping` | Optional cache: `{"crmId": 12}` → site wash id `12` |
| `wash_id` | Sync only one CRM wash (empty = all) |
| `poll_interval` | Seconds (60–120). Site marks wash offline without telemetry after ~3 min; API accepts telemetry ≤ 1/min. |
| `news_limit` | Max news / promotions per wash |

## Install

Dashboard → Automation → Modules → **Washes Nearby** → Install → Settings → Start.

Requires PyOrchestrator (`PYORCHESTRATOR_ENABLED=true`) and CRM washes with `mapsExternalId`.

## Data files

| File | Purpose |
|------|---------|
| `data/wash_mapping.json` | Optional CRM id → site numeric id cache |
| `data/sync_state.json` | Content fingerprints and last telemetry time |
| `data/last_snapshot.json` | Snapshot for UI |
| `data/settings.json` | Module settings |
