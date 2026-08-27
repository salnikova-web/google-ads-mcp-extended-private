---
name: riseguide-ads-ops
description: Use when calling any google-ads MCP tool (even read-only listing) or changing anything in the RiseGuide Google Ads account — budgets, bids, statuses, keywords, negatives, targeting, PMax, recommendations, experiments, campaign cloning. Обов'язково використовувати при будь-якій роботі з інструментами google-ads MCP і при будь-якій зміні в рекламному кабінеті: перелік кампаній, зміна бюджетів/ставок/статусів, ключові й мінус-слова, таргетинг, рекомендації Google, експерименти, клонування кампаній.
---

# RiseGuide: операції в кабінеті (WRITE-шлях)

## Спершу інструмент, потім знання

Питання про кабінет («чи є розбивка за X», «які сегменти/аудиторії
доступні») → СПОЧАТКУ `metadata_get_resource_metadata` або GAQL, і лише
потім загальні знання. «Хочеш, перевіримо?» заборонено — перевіряти
одразу. Демографія/аудиторії → `search_search` по `age_range_view` /
`gender_view` / `campaign_audience_view`, не браузер і не здогадки з
воронки.

Сервер **write-capable**: 16 read-only + 82 write-інструменти. Кожен
write-інструмент має `confirm`, за замовчуванням `false` = dry-run через
`validate_only` (Google валідує повністю, нічого не змінює). **П'ять
інструментів у dry-run не звертаються до Google взагалі** — прев'ю локальне:
`optimize_recommendation_apply/dismiss`, `experiments_experiment_create/end/promote`.

## Обов'язкова процедура будь-якої зміни

Dry-run → показати «було → стане» з переліком усіх зачеплених сутностей
**поштучно** → чекати явного підтвердження в чаті → лише тоді `confirm=true`.
Ніколи не застосовувати без підтвердження. Після застосування — перевірити
результат (superpowers: verification-before-completion).

## Куди дивитись далі

| Питання | Файл |
|---|---|
| Дивна поведінка інструментів, акаунти, клонування, PMax | `references/mcp-quirks.md` |
| Правила безпеки змін: тайминг, ліміти, конфлікти | `references/change-safety.md` |

Аналітика й SQL — скіл `riseguide-ads-analytics`. Нова пастка —
скіл `extracting-lessons`.
