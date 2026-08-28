# google-ads-mcp-extended

Python MCP-сервер `google-ads-mcp` (FastMCP) для Google Ads API:
17 read-only + 84 write-інструменти (101) в 16 неймспейсах. Write-документація —
[README-EXTENDED.md](README-EXTENDED.md).

## КРИТИЧНЕ: ланцюг доставки

- З 28.08.2026 Claude Desktop запускає сервер із бінарника
  `~/.local/bin/google-ads-mcp`, встановленого з **ЦЬОГО репозиторію**:
  `pipx install 'google-ads-mcp @ git+file:///Users/user/Documents/Develop/google-ads-mcp-extended@main'`.
  Живе ТІЛЬКИ закомічене в `main`. Ланцюг оновлення:
  `git commit` → `pipx install --force 'google-ads-mcp @ git+file://…@main'`
  → рестарт Desktop (⌘Q). «Перезапустити MCP-клієнт» — обов'язковий крок,
  клієнт тримає стару версію в пам'яті (граблина, повторена тричі).
- НЕЗАКОМІЧЕНІ правки на живий сервер не впливають ніколи. Так само
  `ads_mcp/tools_config.yaml` бандлиться в інсталяцію — його правки теж
  потребують commit + reinstall.
- Історія: до 28.08 сервер тягнувся `pipx run --spec git+…riseguide/…`
  (інший репозиторій!) без піна — це давало старти 16–61 с і 12 ГБ кешу
  uv. НЕ повертатись до `pipx run`. Пуш у приватний бекап
  `salnikova-web/google-ads-mcp-extended-private` на живий сервер не
  впливає.
- Після `brew upgrade` python сервер може не стартувати →
  `pipx reinstall google-ads-mcp`.
- `.claude/skills/` — частина цього репо: доменні скіли
  `riseguide-ads-analytics` / `riseguide-ads-ops` версіонуються разом
  із кодом.
- Перевірка, чи встановлений пакет: `find_spec('ads_mcp')` запускати
  **з-поза директорії репо** — зсередини cwd дає хибний позитив.
- `claude_desktop_config.json` містить секрети відкритим текстом
  (developer token) — грепати вузько, не тягнути в контекст зайвого.
- Реліз локально: `nox -s deploy` — гейт (чисте дерево, main, black --check,
  тести, pipx install, звірка commit_id) + нагадування про ⌘Q.

## Команди

```bash
nox -s tests          # unittest, py3.10–3.13
nox -s format         # black -l 80
python -m unittest tests.tools.mutate_test.КЛАС.тест   # один тест
```

- **Python-пастка:** на PATH лише python3.14; nox таргетить 3.10–3.13 і
  може тихо запустити НУЛЬ тестів. Робочий обхід:
  `.venv/bin/coverage run -m unittest discover -s tests -p "*_test.py"`.
- Патерн тестових файлів — `*_test.py`, НЕ `test_*.py`. Файл із
  неправильним іменем мовчки не запускається (приклад: історичний
  `tests/smoke/test_token_usage.py` ніколи не бігав під цим іменем —
  перейменований на `tests/smoke/token_usage_check.py`, яке й лишається
  сьогодні).
- `tests/smoke/` не має `__init__.py` → `unittest discover` мовчки
  пропускає ВЕСЬ смоук-пакет (виявлено 28.08: повний прогін «зелений»
  при зламаних goldens). Смоук ганяти окремо:
  `.venv/bin/python -m unittest tests.smoke.smoke_test`. Не додавати
  `__init__.py` мимохідь — це увімкне в discover і llm_test.
- `nox -s lint` історично червоний ще до правок (black не запінений).

## Тести: правила

- Мокати ТІЛЬКИ публічні шви `ads_mcp.utils.get_googleads_client` /
  `get_googleads_service`. НІКОЛИ `_get_googleads_client` — мемоізований
  кеш ликне MagicMock у наступні тести. У `setUp`:
  `utils.clear_googleads_cache()`.
- Смоук-тести діфляться з `tests/smoke/golden_tools_list.json`. Будь-яка
  зміна імені/опису/схеми інструмента ламає їх, поки не перегенеровано:
  `python -m tests.smoke.generate_golden` (або `nox -s update_smoke_golden`)
  — запускати ОДИН раз, останнім, після всіх правок схем (import
  google.genai у скрипті guarded; env `GOOGLE_ADS_MCP_TOOLS_CONFIG`
  smoke_utils пінить сам — недетермінізм goldens закритий). Скриптовий
  шлях `python tests/smoke/generate_golden.py` НЕ працює — падає
  `ModuleNotFoundError: No module named 'tests.smoke'` (`from tests.smoke
  import smoke_utils` вимагає пакетного імпорту, не шляху до файлу;
  перевірено).
- `coordinator.py` монтує інструменти при імпорті — тести патчать
  `ToolsConfig.load` і кличуть `initialize_and_mount_tools` на свіжому
  FastMCP.
- Новий write-інструмент мусить пройти `tests/tools/write_invariants_test.py`
  (рефлексія: кожен `readOnlyHint=False` дефолтить у dry-run).

## Write-безпека і правила правок

- `_preview_or_done` визначено в `ads_mcp/tools/_write_common.py`
  (`mutate.py` ре-експортує для зворотної сумісності) — імпортується
  12 іншими write-модулями + mutate.py, 13 разом. П'ять інструментів не
  мають віддаленої валідації в dry-run: `optimize_recommendation_apply/
  dismiss` (запити БЕЗ поля `validate_only` — не «лагодити»),
  `experiments_experiment_create/end/promote`.
- Назви параметрів у write-інструментах: update-інструменти приймають
  `new_name` (`mutate_ad_group_update`, `pmax_asset_group_update`);
  `mutate_campaign_rename` навмисно лишає параметр `name` — стабільність
  схеми важливіша за однаковість найменувань.
- `mutate_keywords_add`: dry-run атомарний, apply — `partial_failure=True`.
  Незводимі (API відкидає їх разом). Задокументовано, не чіпати.
- `field_mask` губить `""`, `0`, `False` → paths будувати тільки всередині
  наявних `is not None` гілок і тільки leaf-шляхами (не-leaf → 
  `FieldMaskError.FIELD_HAS_SUBFIELDS`). Безумовний `paths=[...]` зітре
  поля, які користувач не передавав.
- `_WRITE_ANNOTATIONS` та спільні write-хелпери централізовано в
  `ads_mcp/tools/_write_common.py` (mutate.py ре-експортує) — правка
  константи напряму зачіпає 74 з 84 write-інструментів (решта 10 мають
  власні inline `ToolAnnotations`, усі 10 з `destructiveHint=True` для
  незворотних дій — REMOVED-статуси, видалення keywords/criteria/asset,
  experiment end тощо); дубль-копії ловить інваріант-тест у
  write_invariants_test.py.
- `search.py` — навмисний raw read-only passthrough GAQL, НЕ вразливість.
  Решта write-шляхів екранують через `gaql_str()`/`gaql_id()`.
- Кеш клієнта: ніколи argless `lru_cache` (у hosted-режимі віддасть виклик
  під чужим токеном); ключ — sha256 токена; ніколи не кешувати
  `get_type()` — повертає мутабельний proto, шаринг ламає конкурентні
  mutate.
- Списки, що живлять exists-check перед записом, ніколи не обрізати тихо:
  `truncated`-прапорець + `ORDER BY`, інакше агент створює дублікати.

## tools_config.yaml

Резолюція: явний шлях → `GOOGLE_ADS_MCP_TOOLS_CONFIG` → `./tools_config.yaml`
у cwd → bundled. Відсутній ключ `namespaces` = усе ввімкнено; порожній
`{}` = нічого (fail-open виправлено у 0.1.0 — не регресувати). Незнайомі
ключі лише варнять — typo (`demand_gen` замість `demandgen`) тихе.

## Стиль, нейтральність, релізи

- PEP8, `black -l 80`. Копірайти: upstream-файли — "Copyright 2026 Google
  LLC." (вимога Apache-2.0), нові — "the google-ads-mcp-extended
  contributors". `[build-system]` у pyproject відсутній навмисно.
- Проєкт нейтральний: без згадок компаній/розробників, плейсхолдери
  `YOUR_ORG`/`example.com`, без AI-трейлерів у комітах.
- Пуш ТІЛЬКИ `main`, ніколи `--all`/`--mirror` — історичні локальні гілки
  можуть нести стару ідентичність авторів (приклад `backup/pre-neutralize`
  вже видалено, але патерн лишається): пушити тільки те, що явно
  призначено для пушу.
- Реліз: branch → PR → merge → tag `vX.Y.Z` → GitHub release → пін
  клієнтського конфігу на `@vX.Y.Z` (не floating main).
- Кожен реліз-PR бампає version у pyproject.toml до тега vX.Y.Z
  (інстальована версія має збігатися з тегом).
- Публічний шеринг — ТІЛЬКИ через розділ «Політика публікації» нижче.
  Видимість ЦЬОГО репо не змінюється ніколи; жодних публічних форків
  від нього.

## Політика публікації (обов'язково — перед будь-яким пушем чи релізом)

- Цей репозиторій ПРИВАТНИЙ і лишається приватним назавжди
  (salnikova-web/google-ads-mcp-extended-private). Його історія містить
  приватний матеріал і ніколи не переписується. Ніколи не пушити жодну
  гілку/тег/об'єкт із цього репо на публічний remote і ніколи не
  змінювати його видимість.
- Репозиторій для шерингу — ОКРЕМИЙ
  (salnikova-web/google-ads-mcp-extended) зі своєю свіжою історією
  (один коміт на реліз). Спільних комітів/blob-ів/тегів із цим репо
  немає і бути не може. Він створений приватним; відкриває його тільки
  Дарина, сама, коли вирішить.
- Публікація ТІЛЬКИ через пайплайн `scripts/publish/`: `export.sh`
  (git archive закоміченого HEAD, export-ignore з `.gitattributes`) →
  `gate.py` (сканер нейтральності) → `release.sh` (clean-room тести,
  синк у клон ~/Documents/Develop/google-ads-mcp-public, коміт, тег).
  Ніколи не копіювати файли в публічний клон руками і не додавати
  публічний remote у цей репо.
- Гейт нейтральності обов'язковий перед КОЖНИМ релізом у репо шерингу.
  Гейт упав чи пропущений → реліз не пушиться. Без винятків і
  оверрайдів.
- Кожен новий трекнутий файл — ТІЛЬКИ англійською і нейтральний: без
  назв компаній/людей, ID акаунтів, внутрішніх хостів/датасетів,
  абсолютних локальних шляхів. Плейсхолдери: YOUR_ORG,
  YOUR_DEVELOPER_TOKEN, 1234567890, example.com, /Users/USERNAME.
  YOUR_ORG у доках НЕ підставляється реальною обліковкою — у вмісті
  файлів прізвище не з'являється ніде (рішення 28.08.2026).
- Приватний/компанійський вміст дозволений ТІЛЬКИ в `CLAUDE.md`,
  `.claude/` і `scripts/` — усі export-ignored (`.gitattributes`) і
  docker-ignored. Новий приватний термін у контексті репо → тим самим
  комітом додати в `scripts/publish/denylist.txt`.
- Уся git-ідентичність клону для шерингу — нейтральна
  `google-ads-mcp-extended contributors <noreply@example.com>`
  (форситься двічі: env у release.sh + git config клону). Merge через
  GitHub UI на репо шерингу ЗАБОРОНЕНИЙ — такий merge-коміт
  підписується реальним ім'ям обліковки (саме так реальне ім'я
  потрапило в історію цього репо, 6 комітів).

## Пастки (симптом → правило → тест, аудит 28.08.2026)

- 45 інструментів мовчки втратили б описи в tools/list: парсер FastMCP
  викидає `LABEL:`-блок з відступним продовженням, якщо блок починає новий
  абзац → продовження тримати на відступі докстрінга → тест виживання
  описів у `schema_test.py` (усі 101, ratio ≥0.85, без тихих скіпів).
- Виняток GAQL «втікав» повз хендлер: gapic `search()` шле page 1 одразу,
  а page 2+ кидає ПІД ЧАС ітерації → і виклик, і ітерація всередині try →
  тести кидають на 2-му рядку через генератор (mutate_test).
- `logger.warning` роками писав у нікуди, а `assertLogs` був зелений:
  NullHandler + жоден хост не конфігурує root → entrypoint сервера сам
  вішає stderr-handler на `"ads_mcp"` → server_test перевіряє РЕАЛЬНУ
  емісію в потік, не assertLogs.
- fastmcp загортає винятки в `ToolError(...) from` ПІД middleware-ланцюгом
  → middleware ходить по `__cause__`; ніде поза `middleware.py` не писати
  `raise ToolError(...) from` → AST-скан `TestChainedToolErrorInvariant`.
- Модель бачить лише name/description/inputSchema: `title`/`idempotentHint`
  мертві, працює тільки `readOnlyHint` (гейтить plan mode Claude Code) —
  його точність тримає біконтіонал-тест (`confirm` ↔ readOnlyHint=False).
- Enum-и в схемах безкоштовні через `Annotated[str, Field(json_schema_extra=
  {"enum": …})]` (+157 Б на 22 параметри); `Literal` заборонений там, де є
  `.upper()`-нормалізація — зламає lowercase-виклики.
- «Який коміт живий?»: `direct_url.json → vcs_info.commit_id` в
  інстальованому dist-info — єдина правда для pipx-VCS-інсталяцій;
  deploy-гейт звіряє його з `git rev-parse HEAD`.

Доменні правила аналітики RiseGuide тут НЕ живуть — вони в персональних
скілах (`~/.claude/skills/`).
