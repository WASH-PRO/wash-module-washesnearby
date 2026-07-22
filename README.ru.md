**Язык:** [English](README.md) · **Русский**

# wash-module-washesnearby

Модуль WASH PRO CRM: синхронизация автомоек с сайтом **«Автомойки рядом»** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## Что делает

1. Создаёт мойку на сайте (название + адрес + координаты), если связи ещё нет
2. Синхронизирует **режимы и цены** — максимум цены режима среди постов мойки
3. Синхронизирует **занятость постов** через telemetry (`free` / `busy` / `broken`)
4. Отправляет **новости** и **акции** из «Публикаций» CRM
5. Отправляет **кассу (finance)** в том же telemetry: `today` / `before_collection` / `after_collection` (наличные / внешние / скидки), по мойке и по постам

## UUID CRM ↔ `external_id`

У каждой мойки в CRM есть **`mapsExternalId`** (UUID v4) → Owner API **`external_id`**. Вызовы: `ext:{uuid}`.

## Касса (finance)

Источник: CRM **`GET /api/crm/finance-stats`** (счётчики MQTT `state/totals` через message-processor).

| Поле Owner API | Источник CRM |
|----------------|--------------|
| `before_collection.{cash,external,discount}` | period `before_collection`: `cash`, `cashless`→`external`, `discountOps`→`discount` |
| `after_collection.*` | period `after_collection` |
| `today.*` | Только из записей **за сегодня** (`recordedAt` в `finance_timezone`). При первом снимке дня: `today ≈ before_collection`. Дальше копится новый оборот. **Вчерашний день не нужен.** |
| `posts[].number` | `posts.postNumber` |
| `date` | Локальный день в `finance_timezone` (по умолчанию `Asia/Yekaterinburg`) |

Правила:

- Нет строк finance-stats у мойки → блок `finance` **не отправляется** (чтобы не затереть историю нулями).
- Итоги мойки = сумма постов.
- Агрегаты **7д / 30д модуль не считает** — их суммирует сайт по дневным `today_*`.
- Касса уходит в том же `PUT .../telemetry`, что и занятость (≤ 1/мин). Выключатель: `sync_finance` (по умолчанию вкл.).
- `GET /api/crm/finance-stats` требует авторизации: модуль логинится в CRM через `SERVICE_LOGIN` / `SERVICE_PASSWORD` (secrets от modules-bridge).

Проверка: `GET /api/v1/owner/car-washes/{id}/finance?period=1d|7d|30d`.

Пример curl:

```bash
export APP=https://xn----7sb0aeimehj.xn--p1ai
export TOKEN=<owner_api_token>
curl --http2 -s -X PUT "$APP/api/v1/integration/washes/ext:<mapsExternalId>/telemetry" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"open","posts":[{"number":"1","status":"free"}],"finance":{"date":"2026-07-22","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":100,"external":200,"discount":10},"after_collection":{"cash":5000,"external":8000,"discount":400},"posts":[{"number":"1","today":{"cash":100,"external":200,"discount":10},"before_collection":{"cash":100,"external":200,"discount":10},"after_collection":{"cash":5000,"external":8000,"discount":400}}]}}'
```

## Настройки

### Общие

| Параметр | Описание |
|----------|----------|
| `owner_api_token` | Bearer-токен владельца |
| `maps_api_base` | URL сайта / API (HTTP/2 через `curl --http2`) |
| `poll_interval` | 60–120 с |
| `news_limit` | Лимит новостей/акций |
| `finance_timezone` | IANA-таймзона для `finance.date` (по умолчанию `Asia/Yekaterinburg`) |

### По мойкам (`washes`)

UI: список моек CRM — `enabled`, широта/долгота, город, тип. Адрес — из карточки мойки.

## Установка

Dashboard → Автоматизация → Модули → **Автомойки рядом** → Установить / Обновить → Настройки → Запустить.

## Тесты

```bash
PYTHONPATH=src python3 -m unittest tests.test_finance -v
```

> PyOrchestrator загружает только `src/main.py` — логика кассы встроена в этот файл.

## Файлы данных

| Файл | Назначение |
|------|------------|
| `data/wash_mapping.json` | Кэш id CRM → числовой id на сайте |
| `data/sync_state.json` | отпечатки, telemetry, baseline кассы за день |
| `data/last_snapshot.json` | снимок для UI |
| `data/settings.json` | настройки |
