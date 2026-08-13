# C: детерминированный formatting — отчёт по главе 0001

- Дата: 2026-08-10 (карточка C, `V4_1_AUDIT_B1_RU.md` §11).
- Источник: `D:/pact/pact_chapters/0001_bonds-1-1.html` (400 блоков, 102 обязательных inline-спанов).
- Перевод (whole-chapter, держит `<em>` 101/101): `0001_bonds-1-1.ru.html`.
- Режим: **model-free** — `run_formatting_align` без caller (правило «formatting = 0 model calls»).

## Результат

- resolved: **100** / 102 (98.0%)
- incidents (unresolved, blocking, debt): **2**
- blocking: `True` (max_formatting_incidents=0)
- model_call_count: **0**
- model_fallback_count: **0**
- тиры: `{'preserved': 100}`
- причины инцидентов: `{'preserved_tag_mismatch': 1, 'target_not_found': 1}`

## Инциденты (unresolved → debt, не тихая потеря)

| PID | span | tier | reason | перевод (фрагмент) |
|---|---|---|---|---|
| p00162 | em01 | preserved | preserved_tag_mismatch | Она вместо этого отступила. Теперь она плакала. — Я думала,  |
| p00183 | em01 | fuzzy | target_not_found | — При всём должном уважении, — сказал я, тщательно подбирая  |

## Вывод

- Форматирование на главе 0001 — **0 вызовов модели**.
- Whole-chapter перевод держит `<em>` 101/101; детерминированный тир `preserved` распознаёт уже-присутствующую разметку и решает ~все спаны без модели (ожидание карточки: ~0 unresolved).
- Любой нерешённый спан — блокирующий инцидент (debt): `accepted_degraded`, никогда тихая потеря и никогда «успех только потому, что 0 model calls».
- Итог на замороженных артефактах: resolved 100, incidents 2, model calls 0.
