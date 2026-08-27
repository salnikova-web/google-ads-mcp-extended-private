# BigQuery: таблиці, схема, пастки даних

## Сервер bigquery-mcp (інфраструктура)

Встановлено персистентно: `pipx install bigquery-mcp` на **Python 3.12**
(uv-managed `~/.local/share/uv/python/cpython-3.12-…`). На Python 3.14
пакет 0.1.6 ПАДАЄ на старті (`ValueError: invalid option name
'--no-vector-search'` — argparse 3.14 суворіший). Оновлення:
`pipx reinstall bigquery-mcp --python <той самий 3.12>`. Конфіг Desktop
вказує на `~/.local/bin/bigquery-mcp` (до 28.08 був `uvx` — старт ~10 с
щоразу; тепер ~2 с).

## Мапа таблиць

| Потрібно | Таблиця |
|---|---|
| Model ROI 7m, денна аналітика | `dbt_google_marts.google_ads_dashboard` |
| Погодинний спенд | `google_ads_hour_dashboard` (немає `model_net_ltv_7`) |
| Гео | `google_ads_geo_dashboard` |

Сусідні датасети: `dbt_meta_marts`, `dbt_payments_marts`, `dbt_marketing_marts` —
крос-канальне порівняння (Meta) і звірка з платежами можливі.

## Схема google_ads_dashboard (робочі колонки)

`campaign_id`, `campaign_name`, `campaign_type` ('DemGen'/'PMax'),
`sql_localization`, `sql_funnel_flow`, `spend`, `model_net_ltv_7`,
`net_ltv_7`, `net_ltv_13`, `revenue`, `purchase`, `leads`, `started_quiz`,
`finish_quiz`, `checkoutView`, `clicks`, `impressions`, `created_date`,
`budget`, `ad_id`, `roi_cac`.

**`checkoutView` — camelCase**, решта snake_case. Легко зробити typo.

Схема зафіксована тут → `get_table` / `list_tables_in_dataset` /
`list_datasets_in_project` НЕ викликати (щосесійне перевідкриття
коштувало ~37 с латентності). `get_table` — лише якщо запит впав на
невідомій колонці. `run_query` завжди з `LIMIT` та/або фільтром дат —
CTE по всій таблиці без дат = повний скан.

Воронка: impressions → clicks → started_quiz → leads → checkoutView →
purchase. Стандартні рейти: click_to_quiz, quiz_to_lead, quiz_to_purch,
co_to_purch, ltv_per_purch.

## Пастки даних

- `created_date` — **дата когорти користувача**, не дата списання.
- Часовий пояс завжди явно: `CURRENT_DATE('Europe/Vienna')`.
- Порожні рядки замість NULL у `gclid`/`gbraid`/`wbraid` →
  `NULLIF(TRIM(field), '')`.
- Різна гранулярність рядків: спенд-рядки мають `ad_id = NULL`
  (fill rate spend ≈ 58%, purchase ≈ 97%). `COUNT(*)` ≠ кількість оголошень.
- Агрегат від агрегату заборонений у `HAVING` — обгортати підзапитом.

## Конвенція імен кампаній

`GG_R13_310726_ENG_DemGen_MC_765_Rizz_allyt_ST_set_Land_onTV`

Сегменти: `GG` платформа · `R13` продукт/потік · **`DDMMYY` дата лончу** ·
локаль (`ENG`) · канал (`DemGen`/`PMax`) · `MC` · воронка (`Rizz`) ·
таргет-токени (`allyt`, `TV`, `Land`, `ST`, `NS`) · бюджет (`bud255`) ·
варіант (`x1`/`x2`). Дата в імені — дешевий крос-чек до
`MIN(created_date)` і спосіб бакетити батчі без джойна.
