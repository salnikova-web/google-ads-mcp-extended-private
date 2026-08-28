# google-ads-mcp-extended

Python MCP-сервер `google-ads-mcp` (FastMCP) для Google Ads API:
16 read-only + 82 write-інструменти в 16 неймспейсах. Write-документація —
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
  uv. НЕ повертатись до `pipx run`. Пуш у `salnikova-web` = бекап, на
  живий сервер не впливає.
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
  неправильним іменем мовчки не запускається (приклад:
  `tests/smoke/test_token_usage.py` — ніколи не бігав).
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
  `python tests/smoke/generate_golden.py` — запускати ОДИН раз, останнім,
  після всіх правок схем. Скрипт має безумовний `import google.genai`
  (потрібен встановлений пакет). Вичистити `GOOGLE_ADS_MCP_TOOLS_CONFIG`
  з env, інакше goldens недетерміновані.
- `coordinator.py` монтує інструменти при імпорті — тести патчать
  `ToolsConfig.load` і кличуть `initialize_and_mount_tools` на свіжому
  FastMCP.
- Новий write-інструмент мусить пройти `tests/tools/write_invariants_test.py`
  (рефлексія: кожен `readOnlyHint=False` дефолтить у dry-run).

## Write-безпека і правила правок

- `_preview_or_done` в `ads_mcp/tools/mutate.py` — імпортується 12 write-
  модулями. П'ять інструментів не мають віддаленої валідації в dry-run:
  `optimize_recommendation_apply/dismiss` (запити БЕЗ поля `validate_only` —
  не «лагодити»), `experiments_experiment_create/end/promote`.
- `mutate_keywords_add`: dry-run атомарний, apply — `partial_failure=True`.
  Незводимі (API відкидає їх разом). Задокументовано, не чіпати.
- `field_mask` губить `""`, `0`, `False` → paths будувати тільки всередині
  наявних `is not None` гілок і тільки leaf-шляхами (не-leaf → 
  `FieldMaskError.FIELD_HAS_SUBFIELDS`). Безумовний `paths=[...]` зітре
  поля, які користувач не передавав.
- `_WRITE_ANNOTATIONS` оголошено окремо в кожному write-модулі (13 шт.) —
  правка константи зачіпає всі ~15 інструментів файлу.
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
- Пуш ТІЛЬКИ `main`, ніколи `--all`/`--mirror` — локальна гілка
  `backup/pre-neutralize` несе стару ідентичність авторів.
- Реліз: branch → PR → merge → tag `vX.Y.Z` → GitHub release → пін
  клієнтського конфігу на `@vX.Y.Z` (не floating main).
- Кожен реліз-PR бампає version у pyproject.toml до тега vX.Y.Z
  (інстальована версія має збігатися з тегом).
- Перед будь-якою зміною видимості репо/публічним форком: `git
  ls-files CLAUDE.md .claude/` у published tree має бути порожнім.

## Відомі хвости (не «лагодити» мимохідь, окремі задачі)

- Stale docstring `mutate.py:166–168`: каже, що PMax asset groups не
  підтримуються — неправда, є неймспейс `pmax`.
- Бандл-скіл `ads_mcp/skills/account-performance-diagnostics/` не пакується
  (MANIFEST.in без `.md`) і має 2 дефекти: `metrics.conversion_value` →
  правильно `conversions_value`; непрефіксовані імена інструментів
  (`search` → `search_search`, `get_resource_metadata` →
  `metadata_get_resource_metadata`).

Доменні правила аналітики RiseGuide тут НЕ живуть — вони в персональних
скілах (`~/.claude/skills/`).
