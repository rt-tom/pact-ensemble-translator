# B9 — офлайн-валидация авто-промоута: dry-run на артефактах run_005 (глава 0001)

- Дата: 2026-08-06
- Ветка: `vk/v4-b9-offline-validation`
- Карточка: `t_0251f40c` (B9-офлайн-валидация, gate B9 §6 — проверка ДО первого
  боевого book-run, когда промоут впервые затронет production `glossary.json`)
- Характер: dry-run, **0 модельных вызовов, 0 HTTP**; детерминированно
- Харнесс: `tests/pact_v4/test_b9_offline_validation.py` (env-var driven,
  `PACT_B9_RUN005_DIR` / `PACT_B9_CHAPTER_HTML` / `PACT_B9_MEMORY_DIR`,
  скипается без артефактов — паттерн B12); контракт путей —
  `tests/pact_v4/test_b9_validation_paths.py`

## Входы (только чтение)

- `run_005_remote` (глава 0001, терминальный статус `accepted_degraded`):
  - `repair_report.json` → `final_translation` (полный, 400 pids) — источник
    финального перевода, нормализованный как в проде (B13/B14:
    `_normalize_final_markup`);
  - `chunk_plan.json` (400 pids, авторитетный: без дубликатов владения);
  - `selection_results.json` → quarantined: `chunk0005`, `chunk0016`;
  - `strict_chapter_trial_record.json` → `step8.status = accepted_degraded`.
- Исходник главы: `0001_bonds-1-1.html` (400 pids, все в плане).
- Production memory (только чтение): `glossary.json` (119 записей),
  `book_memory.json`.

## Метод

Воспроизводит ровно то, что делает `v4_book_run.run_book` между строгим
прогоном и `MemoryManager.promote`, но на **temp-копиях**:

1. temp out-dir: `translations.json` = нормализованный
   `repair_report.final_translation` (400 pids) + копии `chunk_plan.json` /
   `selection_results.json`;
2. temp memory-dir: копии production `glossary.json` + `book_memory.json`;
3. `_generate_and_align_chapter` (исключения B5 allowlist: bible + glossary +
   source-derived; quarantined pids исключены ДО генерации, B9-RV3; fail-closed
   B9-F5/F6 — план авторитетен и полон, генерация прошла);
4. `GlossaryCandidateLedger.append_chapter("0001", aligned)` — во временный
   файл;
5. `_auto_promote_glossary` (v3-пороги + кумулятивный target-конфликт B9-F3 +
   established-конфликт) → proposed/conflicts;
6. `manager.promote("accepted_degraded", quarantined_chunks=…)` +
   `_flatten_promoted_glossary` — в temp memory-dir;
7. committed = diff ключей glossary до/после.

## Результаты

| метрика | значение |
|---|---|
| generated (aligned) | **273** (6 proper_name, 267 term) |
| proposed | **6** (все proper_name) |
| committed | **6** (== proposed; валидный план) |
| conflicts | **186** (alignment-конфликты; co-occurrence guard сработал) |
| term с target, но НЕ proposed | 65 (нужна 2-я глава: `term_min_chapters=2`) |
| term без target | 202 |

### Proposed (6) — что промоутнулось бы в temp glossary

| source | kind | occ | target | consensus | вердикт |
|---|---|---|---|---|---|
| Chris | proper_name | 3 | Крисом | 1.00 | ⚠ target в косвенном падеже (Крисом, творит.) |
| Ivy | proper_name | 8 | Айви | 1.00 | ✅ корректно |
| Jacob | proper_name | 2 | Якобс | 1.00 | ⚠ из «Jacob's Bell» (топоним) |
| **Master** | proper_name | 3 | **Блэйк** | 1.00 | ❌ **ЛОЖНОЕ СРАБАТЫВАНИЕ** |
| Rich | proper_name | 4 | Рич | 1.00 | ✅ корректно |
| Steph | proper_name | 3 | Стеф | 1.00 | ✅ корректно |

## Найденные дефекты (ложные срабатывания) — file:line

### HIGH 1. Титул «Master» → имя «Блэйк» (`pact_v4/phase1/glossary_candidates.py:589-628`)

Контексты главы: «…a liar, Master Blake?», «She even said ‘Master Blake’»,
«I am a lawyer, Master Blake». «Master» — титул-обращение, капитализированный
в середине предложения → стал кандидатом `proper_name`; в переводах
совпавших pids доминирует капитализированное слово **Блэйк** («мастер
Блэйк» — «мастер» строчными, поэтому фильтр capitalized-вариантов его
отбрасывает, а «Блэйк» набирает 3/3). Консенсус 1.00 → авто-промоут
`Master → Блэйк` в glossary — **неверно** (это не перевод «Master», а
совстречающееся имя; корректный target — «мастер», строчными, который фильтр
proper_name-вариантов не рассматривает в принципе).

Класс: proper_name-выравнивание выбирает капитализированное слово
совпавших pids без проверки, что это именно «перевод кандидата», а не
совстречающееся имя/титул. Co-occurrence guard (B9-F2) применяется только к
term-кандидатам и к общему target'у ДВУХ кандидатов — одиночный
proper_name-титул он не ловит.

### HIGH 2. Term-коллокации: «advantage → получить», «anger → чувствовал», «blonde → стороны» (`glossary_candidates.py:629-668`)

- `advantage` occ=3, variants `{получить: 3, преимущество: 2}` → target
  **получить** (share 1.00). «получить преимущество» = «gain an advantage»:
  глагол-коллокация встречается во ВСЕХ pids кандидата и выигрывает у
  истинного перевода «преимущество» (2/3). Частотный контраст
  (`in_rate/out_rate >= 2`) «получить» проходит, т.к. в non-matching pids
  он редок.
- `anger` occ=3, variants `{чувствовал: 2, злость: 2}` → target
  **чувствовал** (share 1.00) — «felt anger», истинный перевод «злость».
- `blonde` occ=4, variants `{стороны: 3, блондинка: 3}` → target
  **стороны** — «блондинка» истинно, но «стороны» (из «с обеих сторон»)
  набирает больше pids.

Класс: share = dominant/len(examined) игнорирует, что истинный перевод
присутствует в 2–3 из 3 pids; частотный контраст не отличает коллокацию от
перевода. В одно-главном dry-run это НЕ промоутнётся (нужны 2 главы), но в
боевом book-run вторая глава с той же коллокацией закоммитит эти пары в
production glossary.

### MEDIUM 3. Topоним «Jacob’s Bell» → «Якобс» (`glossary_candidates.py:407-409` + `:589-628`)

«Jacob» встречается только в составе «Jacob's Bell» (город). Possessive
`'s` срезается → кандидат «Jacob»; target «Якобс» — из перевода топонима
(«Якобс-Бэлл»/«Якобса»). В glossary ушла бы запись `Jacob → Якобс` вместо
топонима целиком. Аналогичный класс: source-токен — фрагмент составного
имени.

### LOW 4. Target в косвенном падеже («Chris → Крисом», «Jacob → Якобс») (`glossary_candidates.py:502-504`, `:618-620`)

`_pick_display_form` берёт самую частотную исходную форму stem'а — если все
вхождения в главе в творительном/родительном падеже, target становится
инфлектированным. Glossary-запись должна быть в именительном.

## Проверенные защиты (работают корректно)

- **Co-occurrence guard (B9-F2)** — сработал на реальных данных: `arms` и
  `hands` обе доминировали на «руки» → обе лишились target
  (conflicts=['руки', …], share=0). Ложная пара не промоутнулась.
- **Quarantined fail-closed (B9-F5/F6) + RV3** — run_005
  `accepted_degraded`, quarantined `chunk0005`/`chunk0016` (58 pids):
  план авторитетен (400 pids, без дубликатов, все source/translation pids
  отображены) → генерация прошла; ни один кандидат не несёт quarantined
  chunk_id (assert в тесте).
- **B5 mixed-script allowlist** — bible/glossary/source-derived токены
  исключены из кандидатов (Blake, Thorburn, … не появляются).
- **Кумулятивный ledger target-конфликт (B9-F3)** — при одной главе не
  проявляется (нет второго chapter target); механика покрыта unit-тестами.
- **Dry-run гарантия**: SHA-256 production `glossary.json` и
  `book_memory.json` до/после идентичны
  (`cefb42d4…07071`, `971ccf4e…d245e56`).

## Вывод

**B9 НЕ готов к боевому book-run с авто-промоутом в текущем виде.**

Механика цепочки (кандидаты → выравнивание → ledger → промоут), quarantined
fail-closed, co-occurrence guard и B5 allowlist работают детерминированно и
безопасно (production memory не тронута; committed == proposed на валидном
плане). Однако консенсус-выравнивание даёт классы ложных срабатываний,
которые при много-главной аккумуляции попадут в production glossary:

1. proper_name-титул → совстречающееся имя (`Master → Блэйк`);
2. term-коллокации побеждают истинный перевод (`advantage → получить`,
   `anger → чувствовал`, `blonde → стороны`);
3. фрагменты составных имён (`Jacob → Якобс`);
4. инфлектированные формы как target (`Chris → Крисом`).

Рекомендация: до первого боевого book-run нужна отдельная правка
выравнивания (после ревью данной карточки) — минимум: (а) отбрасывать
proper_name-кандидата, если его target — уже established glossary-значение
другого ключа (Блэйк — значение ключа Blake) или кандидат не входит в
matching pids с собственным переводом; (б) для term — учитывать долю
истинного перевода, а не только dominant/len(examined). Либо запускать
первый book-run с авто-промоутом в режиме наблюдения (proposed без commit).

## Как воспроизвести

```text
PACT_B9_RUN005_DIR="D:/pact/gate_bench_runs/v4_phase12_strict_0001/run_005_remote" \
PACT_B9_CHAPTER_HTML="D:/pact/pact_chapters/0001_bonds-1-1.html" \
PACT_B9_MEMORY_DIR="D:/pact/pact_chapters" \
py -3.14 -m pytest tests/pact_v4/test_b9_offline_validation.py -q
```

Отчёт (JSON) пишется в `PACT_B9_REPORT_OUT` при наличии.
