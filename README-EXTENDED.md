# google-ads-mcp-extended

Розширення Google Ads MCP-сервера: до read-only інструментів
додані **write-інструменти** для керування кампаніями.

## Нові інструменти (namespace `mutate`)

| Інструмент | Що робить |
|---|---|
| `mutate_campaign_create` | Створює кампанію + окремий денний бюджет. Типи: SEARCH, DISPLAY, SHOPPING, VIDEO, PERFORMANCE_MAX, DEMAND_GEN. Стратегії: Maximize Conversions (+target CPA), Maximize Conversion Value (+target ROAS), Maximize Clicks, Manual CPC. Створюється PAUSED за замовчуванням. |
| `mutate_campaign_update_status` | Пауза / запуск / видалення кампанії (ENABLED, PAUSED, REMOVED). |
| `mutate_campaign_budget_update` | Зміна денного бюджету кампанії. Попереджає, якщо бюджет спільний (shared). |
| `mutate_list_campaigns` | Довідковий (read-only): список кампаній з id, назвою, статусом і бюджетом. |
| `mutate_ad_group_create` / `mutate_ad_group_update` | Створення і редагування груп оголошень (статус, ставка CPC, назва). |
| `mutate_keywords_add` / `mutate_keywords_remove` | Додавання keywords/negative keywords (EXACT/PHRASE/BROAD), видалення за criterion id. |
| `mutate_ad_create_rsa` | Створення Responsive Search Ad (3-15 заголовків, 2-4 описи, валідація лімітів). |
| `mutate_ad_update_status` | Пауза/запуск/видалення оголошення (будь-якого типу). |

## Demand Gen (namespace `demandgen`)

| Інструмент | Що робить |
|---|---|
| `demandgen_asset_upload_image` | Завантаження зображення як Asset (URL або локальний файл, ліміт 5MB). |
| `demandgen_asset_create_youtube_video` | Реєстрація YouTube-відео як Asset. |
| `demandgen_list_assets` | Read-only: пошук наявних asset-ів (IMAGE / YOUTUBE_VIDEO). |
| `demandgen_campaign_create` | DG-кампанія + бюджет (Max Conversions/tCPA, Max Conv Value/tROAS, Max Clicks). |
| `demandgen_campaign_update_bidding` | Зміна tCPA/tROAS існуючої кампанії. |
| `demandgen_ad_group_create` | Група оголошень у DG-кампанії. |
| `demandgen_audience_attach` | Прив'язка існуючої аудиторії до групи. |
| `demandgen_ad_create_image` | Image ad (multi-asset): landscape + square + лого + тексти. |
| `demandgen_ad_create_video` | Video ad: YouTube-відео + заголовки/довгі заголовки/описи + лого. |

## Performance Max (namespace `pmax`)

| Інструмент | Що робить |
|---|---|
| `pmax_campaign_create` | PMax-кампанія + бюджет (Max Conversions/tCPA або Max Conv Value/tROAS). |
| `pmax_campaign_update_bidding` | Зміна tCPA/tROAS існуючої кампанії. |
| `pmax_asset_group_create` | Повний asset group одним запитом: тексти створюються автоматично, картинки/відео — за asset id. Валідація мінімумів Google. |
| `pmax_asset_group_update` | Статус / назва / final URL asset group. |
| `pmax_asset_group_add_texts` | Додавання заголовків/описів у наявний asset group. |
| `pmax_asset_group_add_media` | Прив'язка зображень/відео до asset group. |
| `pmax_asset_group_remove_asset` | Відв'язка asset-а від групи (сам asset лишається в акаунті). |
| `pmax_signal_attach` | Audience signal або search theme для asset group. |
| `pmax_list_asset_groups` | Read-only: asset groups з ad strength і статусами. |

## Запобіжник: dry-run за замовчуванням

Кожен write-інструмент має параметр `confirm`:

- `confirm=false` (за замовчуванням) — запит відправляється з `validate_only=true`:
  Google Ads повністю валідує операцію, але **нічого не змінює**. Повертається прев'ю.
- `confirm=true` — операція застосовується.

Тобто випадкова зміна неможлива: асистент спочатку покаже прев'ю, і лише
після повторного виклику з підтвердженням зміни потраплять в акаунт.

Рекомендація: якщо ваш MCP-клієнт це підтримує, виставити write-інструментам
режим підтвердження перед кожним викликом (**Ask first**).

## Встановлення (кожен користувач)

1. Потрібні: Python 3.10+, pipx (`python3 -m pip install --user pipx`), gcloud CLI.
2. Авторизація (одноразово; потрібен client_secret.json OAuth-клієнта з
   Google Cloud Console вашого проекту):

   ```
   gcloud auth application-default login \
     --client-id-file=/шлях/до/client_secret.json \
     --scopes="https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform"
   ```

3. Блок у конфігурації MCP-клієнта (секція `mcpServers`):

   ```json
   "google-ads": {
     "command": "ШЛЯХ_ДО_PYTHON3",
     "args": [
       "-m", "pipx", "run", "--no-cache",
       "--spec", "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git",
       "google-ads-mcp"
     ],
     "env": {
       "GOOGLE_APPLICATION_CREDENTIALS": "/Users/USERNAME/.config/gcloud/application_default_credentials.json",
       "GOOGLE_PROJECT_ID": "YOUR_GCP_PROJECT_ID",
       "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
       "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
     }
   }
   ```

   `ШЛЯХ_ДО_PYTHON3` — вивід команди `which python3`.
   Для локального тесту без GitHub замініть `--spec` на шлях до цієї папки.

4. Повністю перезапустіть MCP-клієнт (не просто закрийте вікно).

## Що додано до базового сервера

- `ads_mcp/tools/mutate.py` — новий модуль з write-інструментами.
- `ads_mcp/tools_config.yaml` — додано namespace `mutate: true`.
- `ads_mcp/config.py` — `mutate` додано в `ALL_CATEGORIES`.

Все інше — без змін, тому оновлення з upstream підтягуються звичайним merge.

## Extensions (namespace `extensions`)

| Інструмент | Що робить |
|---|---|
| `extensions_add_sitelinks` | Sitelinks для кампанії (текст + URL + описи, валідація лімітів). |
| `extensions_add_callouts` | Callouts (короткі USP-фрази ≤25 символів). |
| `extensions_add_structured_snippets` | Structured snippets (header + 3-10 значень). |
| `extensions_attach_assets` | Прив'язка ІСНУЮЧИХ asset-ів за id (SITELINK/CALLOUT/STRUCTURED_SNIPPET/BUSINESS_NAME/BUSINESS_LOGO) — для клонування без дублювання. |
| `extensions_remove_campaign_asset` | Відв'язка extension-а від кампанії. |
| `extensions_list_campaign_assets` | Read-only: список extensions кампанії з asset id. |

## Targeting (namespace `targeting`) — для всіх типів кампаній

| Інструмент | Що робить |
|---|---|
| `targeting_geo_lookup` | Read-only: пошук geo id за назвами локацій. |
| `targeting_set_locations` | Geo-таргетинг або виключення локацій. |
| `targeting_set_languages` | Мовний таргетинг (за кодами en/de/uk..., id шукаються автоматично). |
| `targeting_set_ad_schedule` | Розклад показів (день + години, у таймзоні акаунта). |
| `targeting_remove_criterion` | Видалення критеріїв таргетингу. |
| `targeting_list_criteria` | Read-only: поточний таргетинг кампанії з criterion id. |

Також: `demandgen_ad_create_carousel` (карусель 2-10 карток) і
`pmax_asset_group_set_all_products` (root listing group для retail-фідів).

## Shopping / Video / Display

- `shopping_campaign_create` (Merchant Center id, feed label, priority) → `shopping_ad_group_create` → `shopping_ad_create_product` → `shopping_ad_group_set_all_products`.
- `video_campaign_create` → `video_ad_group_create` (VIDEO_RESPONSIVE) → `video_ad_create_responsive` (YouTube-відео + тексти).
- `display_campaign_create` → `display_ad_group_create` → `display_ad_create_responsive` (RDA: тексти + зображення + опційно відео/лого/CTA).

## Змінені/додані файли відносно upstream

- `ads_mcp/tools/mutate.py` — write-інструменти Search (кампанії, бюджети, групи, keywords, RSA).
- `ads_mcp/tools/demand_gen.py` — Demand Gen: assets, кампанії, image/video/carousel ads.
- `ads_mcp/tools/pmax.py` — Performance Max: asset groups, signals, listing groups.
- `ads_mcp/tools/extensions.py` — sitelinks, callouts, structured snippets.
- `ads_mcp/tools/targeting.py` — geo, мови, розклад показів.
- `ads_mcp/tools_config.yaml`, `ads_mcp/config.py` — реєстрація namespace-ів.

## Поточні обмеження

- Demand Gen: channel controls підтримані на рівні ad group (demandgen_ad_group_create/update_channels: selected channels або strategy); створення нових комбінованих Audience — через UI.
- PMax: listing groups підтримані лише в режимі «All products» (без дерева підрозділів).
- Редагування текстів існуючого оголошення = створення нового (обмеження Google Ads API — тексти ads незмінні).
- Developer token повинен мати Basic access або вище.

## Tracking / UTM (namespace `tracking`)

`tracking_campaign_set_tracking`, `tracking_account_set_tracking` (tracking_url_template + final_url_suffix), `tracking_list_tracking` (огляд по акаунту і кампаніях).

## Audiences (namespace `audiences`)

`audiences_custom_segment_create` (custom segment з keywords/URLs), `audiences_user_list_create_visitors` (ремаркетинг-лист відвідувачів за URL-правилом), `audiences_campaign_audience_attach`, `audiences_list_audiences`.

## Optimize (namespace `optimize`)

`optimize_recommendations_list` / `recommendation_apply` / `recommendation_dismiss` (Google recommendations), `optimize_change_history` (хто що змінив), `optimize_seasonality_adjustment_create` і `optimize_data_exclusion_create` (підказки Smart Bidding), `optimize_label_create` / `label_apply`.

## Negatives (namespace `negatives`)

`negatives_shared_set_create` / `shared_set_add_keywords` / `attach_to_campaigns` / `list_shared_sets` — спільні списки мінус-слів.

## Experiments (namespace `experiments`)

`experiments_experiment_create` (A/B тест кампанії зі спліт-трафіком), `experiments_list`, `experiment_end`, `experiment_promote`.

## Розширений targeting

`targeting_set_demographics` (виключення віку/статі), `targeting_set_device_bid_modifiers`, `targeting_set_frequency_cap`, `targeting_set_content_exclusions` (brand safety).

## Правила точного клонування кампанії (чекліст)

Виведені з бойового клонування DG-кампанії 19-20.07.2026. Порядок обов'язковий;
кожен шар СПОЧАТКУ читається з кампанії-шаблона через search_search, ПОТІМ
відтворюється. Нічого не залишати "за замовчуванням" — дефолти API ≠ дефолти UI.

**Шар 1 — кампанія:**
1. `campaign`: тип, біддинг (+tCPA/tROAS), бюджет (amount, delivery, shared?).
2. `campaign.tracking_url_template` + `final_url_suffix` + `url_custom_parameters` — КРИТИЧНО, без цього ламається аналітика.
3. `conversion_goal_campaign_config` — custom goal чи customer-дефолти. Якщо custom: `campaign_set_custom_conversion_goal` (він же вимикає категорійні цілі).
4. `campaign_criterion`: LOCATION (+ `geo_target_type_setting` PRESENCE/INTEREST), LANGUAGE, DEVICE, ad schedule, content labels.
5. DG: `demand_gen_campaign_settings.upgraded_targeting` — наш `campaign_create` завжди ставить false (campaign-level гео, як у UI). Прапорець IMMUTABLE — помилку не виправити update-ом, тільки перестворенням.

**Шар 2 — групи оголошень:**
6. `ad_group.demand_gen_ad_group_settings.channel_controls` — режим (SELECTED_CHANNELS чи CHANNEL_STRATEGY) і конкретні канали. Типовий розподіл: video-групи = YT in-stream+in-feed+Shorts; image-групи = ALL_OWNED_AND_OPERATED.
7. `ad_group_criterion` type=AUDIENCE — прив'язка аудиторії (демографія чоловіки/45+ живе ВСЕРЕДИНІ Audience-об'єкта, окремі age/gender критерії не потрібні і будуть відхилені).
8. `ad_group.targeting_setting.target_restrictions` — режим Targeting vs Observation по вимірах (шаблон: AUDIENCE/TOPIC/PLACEMENT=Targeting, GENDER/AGE_RANGE/PARENTAL_STATUS/INCOME_RANGE=Observation) → `targeting_set_ad_group_target_restrictions`.

**Шар 3 — оголошення:**
9. Всі типи ads групи + повний вміст: тексти (headlines/long_headlines/descriptions/business_name/CTA), asset id всіх форматів зображень (landscape + square + PORTRAIT + logo), відео, final_urls (звірити http/https).
10. Статуси: кампанія/групи/ads = PAUSED до ручної перевірки.

**Шар 4 — верифікація (обов'язково):**
11. Пере-прочитати створене і зрівняти з шаблоном: campaign fields, criteria, goals (`biddable=true` має дати 0 рядків при custom goal), channel controls, targeting_setting, кількість ads.

**Відомі граблі API:** category conversion goals за замовчуванням всі увімкнені;
API-створені DG-кампанії мають upgraded_targeting=true; статус REMOVED — тільки
через remove-операцію; ads незмінні (заміна текстів = нове оголошення + пауза старого).

## Відомі межі клонування (перевіряти вручну)

- **Shared budgets**: campaign_create завжди створює окремий бюджет. Якщо шаблон на спільному бюджеті — клон його не успадкує.
- **Portfolio bid strategies**: підтримується лише стандартний біддинг кампанії. Кампанії на портфельних стратегіях клонувати точно не можна.
- **Pinned headlines**: закріплення заголовків за позиціями (pinned_field) у RSA/DG-оголошеннях не переноситься — після клону перевірити пінінг вручну.

## Нове (пп. 4-7)

- `start_date` / `end_date` (YYYY-MM-DD або "YYYY-MM-DD HH:MM:SS") — у всіх campaign_create (mutate, demandgen, pmax, shopping, display); у v24 API це поля `start_date_time`/`end_date_time`.
- `tracking_url_template` на рівні оголошення — mutate_ad_create_rsa, demandgen image/video/carousel, display RDA, video responsive.
- `negative: true` — виключення аудиторій: `demandgen_audience_attach` (рівень групи) і `audiences_campaign_audience_attach` (рівень кампанії).
- `call_to_action` у `demandgen_ad_create_video` — enum-кнопка (LEARN_MORE, SIGN_UP, APPLY_NOW, CONTACT_US, SUBSCRIBE, DOWNLOAD, BOOK_NOW, SHOP_NOW, BUY_NOW, ORDER_NOW, START_NOW, VISIT_SITE, WATCH_NOW, SEE_MORE, PLAY_NOW, GET_QUOTE, DONATE_NOW). Створює CTA-asset і лінкує до оголошення; працює тільки при confirm=true (на dry-run asset не створюється).
- Лейбли кампаній: уже покриті optimize_label_create + optimize_label_apply.

## Retail PMax (Merchant Center) — нюанси клонування

Перевірено на клонуванні retail PMax-кампанії з фідом Merchant Center:

1. **Merchant Center / feed_label** задаються ТІЛЬКИ при створенні (`pmax_campaign_create merchant_id=, feed_label=`). На наявній PMax поле незмінне — для зміни лише перестворення. `mutate_campaign_set_merchant` пробує update, але Google зазвичай відхиляє.
2. **Listing group filter** рідко = «всі товари». Часто дерево по custom_label: `pmax_asset_group_set_listing_filter(custom_label_index, include_values, exclude_others)`. Вимоги API: `listing_source=SHOPPING` на КОЖНОМУ вузлі; кожен SUBDIVISION мусить мати вузол «everything else» (у нас — UNIT_EXCLUDED з порожнім value).
3. **Автозгенеровані асети** (`asset.source = AUTOMATICALLY_CREATED`) НЕ лінкуються вручну (CANNOT_LINK_TO_AUTOMATICALLY_CREATED_ASSET). При копіюванні медіа фільтрувати тільки `ADVERTISER`.
4. **Asset-group persona-аудиторії** (`audience.scope = ASSET_GROUP`, ім'я AssetGroupPersona_*) не перевикористовуються — відтворювати через `audiences_create` з тим самим складом segments і чіпляти як сигнал.
5. **Конверсійні цілі**: якщо `conversion_goal_campaign_config.custom_conversion_goal` порожній — кампанія на акаунт-дефолтних цілях, кастомну НЕ чіпляти.
6. **Geo-тип PMax** дефолтний PRESENCE_OR_INTEREST — якщо оригінал PRESENCE, ставити явно через `mutate_campaign_update_settings positive_geo_target_type=PRESENCE`.
7. **Asset automation** (Image extraction/enhancement, Video enhancement) — звіряти `campaign.asset_automation_settings` і виставляти через `mutate_campaign_update_settings image_extraction/image_enhancement/video_enhancement`.
8. PMax опис: хоча б один ≤60 символів (інакше NOT_ENOUGH / помилка).

## Retail PMax — ще два нюанси (доповнення)

9. **Conversion goals «Campaign-specific»**: якщо `conversion_goal_campaign_config.goal_config_level = CAMPAIGN`, а `custom_conversion_goal` порожній — це стандартні категорійні цілі на рівні кампанії (напр. лише Purchases biddable). Виставляти через `mutate_campaign_set_conversion_goals(biddable_categories=["PURCHASE"])` (перемикає рівень на CAMPAIGN + робить biddable лише вказані категорії). НЕ плутати з custom goal (окремий тул).
10. **LANDSCAPE_LOGO**: у Brand Guidelines окрім LOGO (1:1) буває LANDSCAPE_LOGO (4:1). Обидва — окремі campaign_asset field types; копіювати обидва через `extensions_attach_assets`.
