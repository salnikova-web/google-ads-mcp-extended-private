# Google Ads MCP: квіркси й акаунти

## Акаунти

- `4561421745` — робочий акаунт, кампанії читаються.
- `1268517178` — MCC-менеджер без власних кампаній (порожній список — норма).
- Клієнтські акаунти під MCC **не з'являються** в
  `list_accessible_customers`. Відсутність ≠ немає доступу.
- `list_accessible_customers` не використовує `login-customer-id`, решта
  викликів — так. Перший працює, а дані дають `USER_PERMISSION_DENIED` =
  mismatch `login_customer_id`, не проблема авторизації.

## Списки

- `mutate_list_campaigns` мовчки обрізає на ліміті. Кількість рядків =
  ліміт → результат неповний. Для підрахунків: `search_search` (GAQL),
  `fields=["campaign.id"]`, високий ліміт.
- Ніколи не робити exists-check перед записом по обрізаному списку —
  рядок за лімітом → хибний висновок «не існує» → дублікат.

## Помилки: рецепти

- **`invalid_grant`** (`'invalid_grant: Bad Request'` / `'Token has been
  expired or revoked'`) — НІКОЛИ не транзиентний: один виклик → стоп →
  сказати «перепідключіть конектор» (Desktop: Settings → Connectors) або
  перевірити креденшл. Не ретраїти (23.07: 3 ретраї, 7.8 хв, 0 даних).
  Повторюється після reconnect → перевірити тип креденшла (зараз service
  account — для Ads API потребує domain-wide delegation, відкрите питання).
- **503 `No route to host … ipv6:…`** — локальна IPv6-проблема (10 випадків
  у логах), не API і не запит; лікується на рівні системи.
- **Помилки GAQL** приходять як `Request ID: …` + текст — цей ID для
  підтримки Google.

## GAQL-дисципліна

- `search_search` НЕ має дефолтного ліміту, всі рядки матеріалізуються →
  завжди передавати `limit`.
- Дати тільки `YYYY-MM-DD`, діапазон скінченний з обох боків;
  `LAST_3_DAYS` не існує. `change_event` вимагає `LIMIT <= 10000`.
- `customer_id` — цифри без дефісів.

## Desktop ≠ Claude Code

У Desktop cowork доступні лише 3 read-only інструменти (search_search,
metadata_get_resource_metadata, customers_list_accessible_customers) —
write фізично недоступний. Зміни в кабінеті обіцяти/планувати тільки
з Claude Code.

## Загальні максими

- **Дефолти API ≠ дефолти UI.** Те, що кабінет ставить сам, API не ставить.
- `mutate_list_campaigns` сортує за status+name, НЕ за спендом — «топ за
  спендом» ним не отримати (тільки search_search + ORDER BY metrics);
  `daily_budget` вже конвертований з мікро — не ділити вдруге.
- `login_customer_id` задається тільки env (`GOOGLE_ADS_LOGIN_CUSTOMER_ID`),
  з виклику не лікується: правка конфігу + рестарт Desktop.

## Сервер: звідки код і як оновлювати

Сервер встановлено з ЛОКАЛЬНОГО репо:
`pipx install 'git+file:///Users/user/Documents/Develop/google-ads-mcp-extended@main'`.
Живе ТІЛЬКИ закомічене в main. Оновлення: `git commit` →
`pipx install --force 'git+file://…@main'` → рестарт Desktop (⌘Q).
Після `brew upgrade` python сервер може не стартувати →
`pipx reinstall google-ads-mcp`. Перший виклик сесії може чекати
на старт сервера (~2–4 с після фіксу; історично 16–61 с через
unpinned `pipx run` — не повертатись до нього).

`uv cache clean` — тільки при закритому Desktop: uvx-сервери (bigquery)
виконуються прямо з файлів кешу, чистка під ними або впреться в
`.lock`-timeout, або висмикне файли з-під живого процесу.

## Клонування кампаній

Повний чек-лист (11 кроків, 4 шари) — `README-EXTENDED.md:349–440` у репо.
Ключове: **не відтворюються** shared budgets, portfolio bid strategies,
pinned headlines. Retail PMax: `merchant_id` незмінний після створення;
`listing_source=SHOPPING` на кожному вузлі; кожен SUBDIVISION потребує
вузла "everything else"; `AUTOMATICALLY_CREATED` асети не лінкуються;
`AssetGroupPersona_*` аудиторії не перевикористовуються.
