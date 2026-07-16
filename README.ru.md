**Язык:** [English](README.md) · **Русский**

# wash-module-washesnearby

Модуль WASH PRO CRM: синхронизация автомоек с сайтом **«Автомойки рядом»** ([Owner Integration API](https://github.com/Developer-RU/WASH-PRO-MAPS/blob/main/docs/04-partner-ingest-api.md)).

## Что делает

1. Создаёт мойку на сайте (название + адрес + координаты), если связи ещё нет
2. Синхронизирует **режимы и цены** — для каждого режима берётся **максимальная** цена среди всех постов мойки (названия — из справочника режимов CRM; у постов одной мойки названия режимов одинаковые)
3. Синхронизирует **занятость постов** через telemetry (`free` / `busy` / `broken`)
4. Отправляет последние **новости** и **акции** из раздела CRM «Публикации»

## Настройки

| Параметр | Описание |
|----------|----------|
| `owner_api_token` | Bearer-токен владельца (кабинет → API) |
| `maps_api_base` | Базовый URL API (по умолчанию punycode `https://xn----7sb0aeimehj.xn--p1ai` = мойка-про.рф). Сайт принимает Owner API по HTTP/2 — модуль вызывает его через `curl --http2`. |
| `default_latitude` / `default_longitude` / `default_city` | Для создания мойки (в CRM только текстовый адрес) |
| `wash_coords` | JSON по мойкам: `{"crmId":{"lat":55.16,"lng":61.4,"city":"…"}}` |
| `wash_mapping` | Предварительная связь: `{"crmId": 12}` → id мойки на сайте |
| `wash_id` | Только одна мойка CRM (пусто = все) |
| `poll_interval` | Секунды (мин. 60 — telemetry не чаще 1 раза в минуту) |
| `news_limit` | Сколько последних новостей/акций отправлять |

## Как сопоставить мойку заранее

В Integration API **нет** поля внешнего CRM id при создании. Варианты:

1. **Автоматически** — модуль создаёт мойку и пишет `crmId → remoteId` в `data/wash_mapping.json`
2. **Вручную** — в настройках `wash_mapping`, например `{"64f…": 3}`, если мойка уже есть на сайте
3. **Улучшение API** — сохранять `metadata.crm_wash_id` на стороне сайта (сейчас в validate create не принимается)

## Установка

Dashboard → Автоматизация → Модули → **Автомойки рядом** → Установить → Настройки → Запустить.

Нужен PyOrchestrator (`PYORCHESTRATOR_ENABLED=true`).

## Файлы данных

| Файл | Назначение |
|------|------------|
| `data/wash_mapping.json` | id мойки CRM → id на сайте |
| `data/sync_state.json` | отпечатки контента и время последней telemetry |
| `data/last_snapshot.json` | снимок для UI |
| `data/settings.json` | настройки модуля |
