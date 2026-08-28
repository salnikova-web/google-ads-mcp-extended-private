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

- Усі 12 list-style read-інструментів (`mutate_list_campaigns`,
  `demandgen_list_assets`, `optimize_recommendations_list`,
  `optimize_change_history`, `mutate_keywords_ideas`,
  `extensions_list_campaign_assets`, `targeting_list_criteria`,
  `negatives_list_shared_sets`, `tracking_list_tracking`,
  `experiments_experiments_list`, `pmax_list_asset_groups`,
  `audiences_list_audiences`) повертають конверт `{items|<ключ>,
  returned, truncated}` і додають рядок `warning`, коли truncated=true.
  Мовчазного обрізання більше немає.
- Truncated ≠ повний список. Відсутність рядка в обрізаному списку ≠
  «не існує» — рядок міг лишитись за лімітом. При truncated: підняти
  `limit` або звузити фільтр, і переказати `warning` користувачці перед
  тим, як робити висновок про відсутність (сортування за спендом —
  дивись «Загальні максими» нижче).

## Батч-мутації (≥3 кампаній)

- Для ≥3 кампаній — не по одній: `mutate_campaign_update_status_batch`
  (лише ENABLED/PAUSED; REMOVED незворотний, тому batch його відмовляє —
  видаляти по одній через `mutate_campaign_update_status`) і
  `mutate_campaign_budget_update_batch` (спільні бюджети групуються в
  одну операцію на бюджет; кампанії на одному спільному бюджеті з РІЗНИМИ
  сумами в одному виклику падають ДО будь-якого запису — нічого не
  міняється).
- Обидва: dry-run (`confirm=false`) — один атомарний запит, будь-яка
  погана id валить увесь прев'ю; apply (`confirm=true`) —
  `partial_failure=true` з результатом по кожній кампанії окремо
  (`requested`/`succeeded`/`failed`).

## Помилки: рецепти

- **`invalid_grant`** (`'invalid_grant: Bad Request'` / `'Token has been
  expired or revoked'`) — НІКОЛИ не транзиентний: один виклик → стоп →
  сказати «перепідключіть конектор» (Desktop: Settings → Connectors) або
  перевірити креденшл. Не ретраїти (23.07: 3 ретраї, 7.8 хв, 0 даних).
  Повторюється після reconnect → перевірити тип креденшла (зараз service
  account — для Ads API потребує domain-wide delegation, відкрите питання).
  Сервер v0.3.0 сам перекладає це в понятну відповідь (middleware): агент
  отримує «NOT retryable — re-authenticate», а не сирий traceback —
  порада «не ретраїти, перепідключити конектор» лишається чинною, просто
  вже без потреби розбирати трасу самостійно.
- **503 `No route to host … ipv6:…`** — локальна IPv6-проблема (10 випадків
  у логах), не API і не запит; лікується на рівні системи.
- **Помилки GAQL** приходять як `Request ID: …` + текст — цей ID для
  підтримки Google. `UNRECOGNIZED_FIELD`/`PROHIBITED_FIELD` тепер несуть
  готову підказку: звірити поле через `metadata_get_resource_metadata`;
  той інструмент сам відсилає далі до нового `metadata_get_field_details`
  (тип поля, enum-значення, з чим поле можна селектити) — не гадати назву
  вручну.

## GAQL-дисципліна

- `search_search`: дефолтний `limit=1000` (діє, якщо `limit` не передано
  взагалі). Явний `limit=null` — повний експорт без обрізання. При
  обрізанні (дефолтом чи явним лімітом) відповідь несе чесний `warning` і
  `total: null` (розмір усього набору невідомий, поки він не вичерпаний).
  Для `change_event` додатково можливий `api_row_cap_hit: true` — API сама
  не дає читати більше 10000 рядків за запит, і в такому разі `total`
  теж лишається null (стеля, а не вичерпання).
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

`nox -s deploy` (гейт релізу) інколи відмовляє на кроці `pipx install`:
«virtual environment already exists … not created in this session»
(pipx на uv-бекенді). Перезапустити як `UV_VENV_CLEAR=1 nox -s deploy` —
перевірено 28.08.2026 на деплої v0.3.0.

**Цілісність fastmcp під питанням (виявлено 28.08.2026):** у всіх venv цієї
машини (робочий, dev, pipx) fastmcp 3.4.7 має обрізаний dist-info RECORD
(5 рядків замість переліку файлів), а свіже `pip download --no-cache-dir`
з PyPI віддає wheel БЕЗ коду (лише метадані). Поведінково на сервер не
впливає, але перед будь-яким апгрейдом fastmcp — звірити хеші wheel на
pypi.org незалежним каналом (браузером), не довіряти локальному pip.

## Клонування кампаній

Повний чек-лист (11 кроків, 4 шари) — `README-EXTENDED.md:430–486` у репо
(секція «Campaign cloning checklist»).
Ключове: **не відтворюються** shared budgets, portfolio bid strategies,
pinned headlines. Retail PMax: `merchant_id` незмінний після створення;
`listing_source=SHOPPING` на кожному вузлі; кожен SUBDIVISION потребує
вузла "everything else"; `AUTOMATICALLY_CREATED` асети не лінкуються;
`AssetGroupPersona_*` аудиторії не перевикористовуються.
