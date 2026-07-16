**Язык:** [English](README.md) · **Русский**

# wash-module-washesnearby

Модуль WASH PRO CRM: синхронизация автомоек с сайтом **«Автомойки рядом»** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## Что делает

1. Создаёт мойку на сайте (название + адрес + координаты), если связи ещё нет
2. Синхронизирует **режимы и цены** — для каждого режима берётся **максимальная** цена среди всех постов мойки (названия — из справочника режимов CRM; у постов одной мойки названия режимов одинаковые)
3. Синхронизирует **занятость постов** через telemetry (`free` / `busy` / `broken`)
4. Отправляет последние **новости** и **акции** из раздела CRM «Публикации»

## UUID CRM ↔ `external_id` на сайте

У каждой мойки в CRM есть поле **`mapsExternalId`** (UUID v4). Модуль передаёт его на сайт как Owner API **`external_id`**:

| Шаг | Что происходит |
|-----|----------------|
| Создание мойки в CRM | Dashboard назначает `mapsExternalId` |
| Старые мойки | `init-seed` дописывает UUID, если поля нет |
| Синхронизация модуля | Ищет / создаёт / обновляет мойку на сайте по этому UUID |
| Вызовы API | `PATCH` и telemetry идут как `ext:{uuid}` |

CRM **не зависит** от числового id мойки на сайте. Файл `data/wash_mapping.json` — только кэш.

Если у мойки нет `mapsExternalId`, модуль пропускает её с ошибкой — обновите CRM / выполните `init-seed` или откройте мойку в Dashboard и сохраните.

## Настройки

| Параметр | Описание |
|----------|----------|
| `owner_api_token` | Bearer-токен владельца (кабинет → API) |
| `maps_api_base` | Базовый URL API (по умолчанию punycode `https://xn----7sb0aeimehj.xn--p1ai` = мойка-про.рф). Сайт принимает Owner API по HTTP/2 — модуль вызывает его через `curl --http2`. |
| `default_latitude` / `default_longitude` / `default_city` | Для создания мойки (в CRM только текстовый адрес) |
| `wash_coords` | JSON по мойкам: `{"crmId":{"lat":55.16,"lng":61.4,"city":"…"}}` |
| `wash_mapping` | Опциональный кэш: `{"crmId": 12}` → id мойки на сайте |
| `wash_id` | Только одна мойка CRM (пусто = все) |
| `poll_interval` | Секунды (60–120). На сайте мойка offline без телеметрии ~3 минуты; API принимает telemetry ≤ 1/мин. |
| `news_limit` | Сколько последних новостей/акций отправлять |

## Установка

Dashboard → Автоматизация → Модули → **Автомойки рядом** → Установить → Настройки → Запустить.

Нужен PyOrchestrator (`PYORCHESTRATOR_ENABLED=true`) и UUID у моек CRM (`mapsExternalId`).

## Файлы данных

| Файл | Назначение |
|------|------------|
| `data/wash_mapping.json` | Опциональный кэш id CRM → числовой id на сайте |
| `data/sync_state.json` | отпечатки контента и время последней telemetry |
| `data/last_snapshot.json` | снимок для UI |
| `data/settings.json` | настройки модуля |
