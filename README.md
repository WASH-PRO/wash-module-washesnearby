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

Source: CRM **`GET /api/crm/finance-stats`** (MQTT `state/totals` upserted by message-processor).

| Owner field | CRM source |
|-------------|------------|
| `before_collection.{cash,external,discount}` | period `before_collection`: `cash`, `cashless`→`external`, `discountOps`→`discount` |
| `after_collection.*` | period `after_collection` (same field map) |
| `today.*` | Day delta of **`after_collection`** vs baseline in `data/sync_state.json` (CRM has no daily period) |
| `posts[].number` | `posts.postNumber` |
| `date` | Local calendar day in `finance_timezone` (default `Asia/Yekaterinburg`) |

Rules:

- If a wash has no finance-stats rows → **omit** `finance` (do not wipe history with zeros).
- Wash-level buckets = sum of posts.
- **Do not** send 7d/30d aggregates — the site sums daily `today_*` itself.
- Finance rides in the same `PUT .../telemetry` as occupancy (≤ 1/min). Toggle with env/setting `sync_finance` (default on).

Owner check after sync: `GET /api/v1/owner/car-washes/{id}/finance?period=1d|7d|30d`.

Example telemetry curl:

```bash
export APP=https://xn----7sb0aeimehj.xn--p1ai
export TOKEN=<owner_api_token>
curl --http2 -s -X PUT "$APP/api/v1/integration/washes/ext:<mapsExternalId>/telemetry" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"open","posts":[{"number":"1","status":"free"}],"finance":{"date":"2026-07-22","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":100,"external":200,"discount":10},"after_collection":{"cash":5000,"external":8000,"discount":400},"posts":[{"number":"1","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":100,"external":200,"discount":10},"after_collection":{"cash":5000,"external":8000,"discount":400}}]}}'
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
