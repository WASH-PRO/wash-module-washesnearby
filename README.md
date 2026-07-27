**Language:** English (default) · [Русский](README.ru.md)

# wash-module-washesnearby

WASH PRO CRM module: sync car washes to **Washes Nearby** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## What it does

1. Creates a wash on the map site (name + address + coordinates) if there is no mapping yet
2. Syncs **service modes / prices** — for each work mode, takes the **maximum** price across all posts of that wash
3. Syncs **post occupancy** via telemetry (`free` / `busy` / `broken`)
4. Syncs latest **news** and **promotions** from CRM Publications
5. Syncs **cash register (finance)** in the same telemetry: `today` / `before_collection` / `after_collection` (cash / external / discount), wash + per post

## CRM UUID ↔ site `external_id`

Each CRM wash has **`mapsExternalId`** (UUID v4). The module sends it to the site as Owner API **`external_id`**. API calls use `ext:{uuid}`.

## Finance (cash register)

| Owner field | Source |
|-------------|--------|
| `today.cash` / `external` / `discount` | sum of MQTT `state/credit` for **today** (`type` 0/1/2) |
| `before_collection` | live CRM `finance-stats` period `before_collection` (acash / anoncash / adiscount) |
| `after_collection` | same for `after_collection` (tcash / tnoncash / tdiscount) |
| `posts[].number` | `posts.postNumber` (payments matched via `serialNumber`) |
| `date` | Local calendar day in `finance_timezone` (default `Asia/Yekaterinburg`) |

Rules:

- **`today`** — today’s payments only; 7d / 30d are summed by the site from daily `today_*`.
- **`before` / `after`** — live panel counters (not day-scoped); shown on the site’s live finance block.
- Wash totals = sum of posts. No posts → omit `finance`.
- Even with no payments yet, send `today=0` (with current before/after).
- Finance rides in the same `PUT .../telemetry` as occupancy (≤ 1/min). Toggle: `sync_finance`.
- CRM auth required: `SERVICE_LOGIN` / `SERVICE_PASSWORD` (modules-bridge secrets).

Owner check: `GET /api/v1/owner/car-washes/{id}/finance?period=1d|7d|30d`.

Example telemetry curl:

```bash
export APP=https://xn----7sb0aeimehj.xn--p1ai
export TOKEN=<owner_api_token>
curl --http2 -s -X PUT "$APP/api/v1/integration/washes/ext:<mapsExternalId>/telemetry" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"open","posts":[{"number":"1","status":"free"}],"finance":{"date":"2026-07-22","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":1300,"external":12550,"discount":64},"after_collection":{"cash":0,"external":0,"discount":0},"posts":[{"number":"1","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":1300,"external":12550,"discount":64},"after_collection":{"cash":0,"external":0,"discount":0}}]}}'
```

## Settings

### Common

| Key | Description |
|-----|-------------|
| `owner_api_token` | Owner API Bearer token |
| `maps_api_base` | Site / API URL (HTTP/2 via `curl --http2`) |
| `poll_interval` | 60–120 s |
| `news_limit` | Max news / promotions per wash |
| `finance_timezone` | IANA TZ for `finance.date` (default `Asia/Yekaterinburg`) |

### Per wash (`washes`)

UI lists CRM washes; each has `enabled`, `latitude`, `longitude`, `city`, `type`. Address comes from the CRM wash card.

## Install

Dashboard → Automation → Modules → **Washes Nearby** → Install / Update → Settings → Start.

Requires PyOrchestrator and CRM washes with `mapsExternalId`.

## Tests

```bash
PYTHONPATH=src python3 -m unittest tests.test_finance -v
```

> PyOrchestrator uploads only `src/main.py` — finance helpers live in that file (not a separate module).

## Data files

| File | Purpose |
|------|---------|
| `data/wash_mapping.json` | Optional CRM id → numeric site id cache |
| `data/sync_state.json` | Content fingerprints, telemetry time, finance day baseline |
| `data/last_snapshot.json` | UI snapshot |
| `data/settings.json` | Module settings |
