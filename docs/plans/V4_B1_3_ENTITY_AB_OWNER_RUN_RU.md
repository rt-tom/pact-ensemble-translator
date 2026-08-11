# B1.3 — Entity-context A/B + 8 кейсов: прогон владельца (owner-run)

> Карточка: t_97556aa7 (B1.3 spike, decision gate). Конспект:
> `docs/plans/V4_1_AUDIT_B1_RU.md` §9 + §10 B1.3. Код:
> `pact_v4/audit/b13_ab.py` (SPIKE, НЕ production-путь; не экспортируется
> из `pact_v4.audit.__init__`). Тесты: `tests/pact_v4/audit/test_b13_ab.py`.
>
> **Правило владельца 2026-08-06:** реальные Qwen-прогоны запускает ТОЛЬКО
> владелец, вручную, вне чата. Агент (developer) НЕ запускает Qwen.
> Developer подготовил харнесс, 8 фикстур, метрики, mock-валидацию
> (0 вызовов Qwen) и эти команды.

## 1. Что делает прогон

A/B на ОДИНАКОВЫХ чанках главы 0001 (тот же перевод —
`run_006_local_gemma/translations.json`), три конфигурации entity-context:

| Конфиг | entity_context | Источник |
|---|---|---|
| `none` | пусто | — |
| `gold` | `chapter_entity_context_0001.txt` (эталон) | ручной |
| `auto` | B1.2 extractor на source главы (1 вызов Qwen) | авто |

Чанки идентичны во всех трёх конфигурациях (greedy, max_input=3600 →
ровно 8 чанков главы 0001) — любое различие в исходах вызвано ТОЛЬКО
блоком entity-context (изолированный эксперимент).

Промпт v4.1 и `ChunkedAuditEvaluator` НЕ изменяются (заморожены); контекст
подаётся только через параметр `entity_context` (test leakage §9.3
исключён — примеры в промпте нейтральные).

## 2. Метрики (карточка §4)

| Метрика | Формула | Смысл |
|---|---|---|
| gold TP recall | `\|issues ∩ gold_tp\| / \|gold_tp\|` (по (pid, category)) | нашли ли известные дефекты |
| gold negative rejection | `\|gold_negative без issues\| / \|gold_negative\|` | отклонили ли заведомо чистые PID |
| new unknown issues | список issues вне gold_tp и вне gold_negative (НЕ число issues) | что нового/шумного конфиг добавляет |

Gold-набор главы 0001 (B1 §6): TP = p00010/p00013/p00032/p00035/p00093/
p00132/p00193/p00236 (с категориями), negative = p00075/p00106/p00136/
p00151/p00184/p00309. Gold-наборы 8 кейсов — в `B13_CASES`
(`pact_v4/audit/b13_ab.py`).

## 3. Команды владельца (PowerShell)

Харнесс сам НЕ стартует llama-server (правило 2026-08-06). Сначала
поднимите Qwen llama-server (как в A1: Qwen3.6-35B-A3B, `-c 49152`,
`--reasoning-budget 8192`, порт 8094 — параметры в
`configs/runtime_local.example.yaml`, секция `qwen`). Проверка готовности:
`curl.exe -s -m 5 http://127.0.0.1:8094/health`.

### 3.1 Реальный A/B главы 0001 (Qwen)

Сначала сгенерируйте source-карту главы 0001 тем же парсером, что и run_006
(результат НЕ хранится в репо — данные глав не коммитятся):

```powershell
cd D:\pact\pact_translator_worktrees\b1-3-entity-ab

C:\Python314\python.exe -c "import sys, json; sys.path.insert(0,'.'); from pathlib import Path; from pact_v4.phase0b.source_html import load_source; from pact_v4.phase1.models import SourceArtifact; blocks,_=load_source(Path(r'D:\pact\pact_chapters\0001_bonds-1-1.html')); sa=SourceArtifact(chapter_id='0001', source=tuple((b.pid,b.text) for b in blocks)); assert sa.source_hash=='719d25c0871bc84f18b2d8e28700f4c531fff48e50597fcf753057dd79843fba', sa.source_hash; Path('_b13_source_0001.json').write_text(json.dumps({b.pid:b.text for b in blocks}, ensure_ascii=False, indent=1), encoding='utf-8'); print('ok', len(blocks))"
```

(Проверка: source_hash обязан совпасть с `strict_chapter_trial_record.json`
run_006 — 719d25c0…; иначе source не тот.)

Затем A/B:

```powershell
C:\Python314\python.exe -m pact_v4.audit.b13_ab `
  --source D:\pact\pact_translator_worktrees\b1-3-entity-ab\_b13_source_0001.json `
  --translation D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_006_local_gemma\translations.json `
  --gold-context "D:\test folder\chapter_entity_context_0001.txt" `
  --out-dir D:\pact\pact_translator_worktrees\b1-3-entity-ab\_b13_out_real `
  --backend real --chat-url http://127.0.0.1:8094/v1/chat/completions `
  --model Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
```

Замечание по source: `_b13_source_0001.json` — PID→source-карта главы 0001,
сгенерированная из `D:\pact\pact_chapters\0001_bonds-1-1.html` тем же
парсером, что и run_006 (SourceArtifact source_hash совпадает с
`strict_chapter_trial_record.json` run_006: 719d25c0…). Если у вас есть
канонический source-артефакт run_006 — подставьте его путь.

Ожидаемые вызовы: `auto` = 1 вызов extractor + 8 чанков; `gold`/`none` =
8 чанков каждый. Итого 25 вызовов Qwen. Время — как обычный chunked audit.

### 3.2 Реальный прогон 8 кейсов §9.1 (Qwen)

Добавьте `--run-cases` к команде §3.1 (реальный бэкенд прогонит каждый из
8 кейсов отдельным вызовом Qwen и запишет per-case метрики в `--out-dir`):

```powershell
C:\Python314\python.exe -m pact_v4.audit.b13_ab `
  --source D:\pact\pact_translator_worktrees\b1-3-entity-ab\_b13_source_0001.json `
  --translation D:\pact\gate_bench_runs\v4_phase12_strict_0001\run_006_local_gemma\translations.json `
  --gold-context "D:\test folder\chapter_entity_context_0001.txt" `
  --out-dir D:\pact\pact_translator_worktrees\b1-3-entity-ab\_b13_out_real `
  --backend real --chat-url http://127.0.0.1:8094/v1/chat/completions `
  --model Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --run-cases
```

Итого вызовов Qwen: 1 extractor (auto) + 3×8 чанков A/B + 8 кейсов = 33.

### 3.3 Что вернуть

Результаты кладутся в `--out-dir`:

* `ab_real.json` — исходы трёх конфигураций (payload `pact-audit/v4`),
* `metrics_real.json` — метрики по gold-наборам главы 0001,
* `ab_*/ab_*_chunkN_raw.txt` + `_reasoning.txt` — сырые артефакты чанков
  (persist для разбора и цитируемости).

Вернуть владельцу/архитектору: путь к `--out-dir`, либо вложить
`ab_real.json` + `metrics_real.json` (без сырых чанков) в карточку.

## 4. Таблицы результатов (заполняются после прогона)

### 4.1 A/B главы 0001 (реальные цифры — прогон владельца 2026-08-10)

> **ВАЖНО:** `metrics_real.json` невалиден (gold в харнессе зашит из
> dev-перевода, p00236 — а в run_006 ошибка в p00097). Метрики пересчитаны
> вручную по фактическим TP run_006 (конспект §9.5.1): recall 8/10 во всех
> трёх конфигурациях.

| Конфиг | gold TP recall | neg rejection | new unknown (факт) |
|---|---|---|---|
| none | 8/10 | 0.5 (p00075/p00136/p00184) | p00016/p00322 |
| gold | 8/10 | 0.5 | p00016/p00322 |
| auto | 8/10 | **0.667** (лучший) | p00016/p00322 |

**Вывод:** recall одинаков, но entity-context МЕНЯЕТ профиль ошибок
(помогает p00035/p00240, мешает p00016/p00322); auto НЕ добавляет FP на
реальной главе (rejection лучший).

### 4.2 8 кейсов §9.1 — реальный Qwen (прогон владельца 2026-08-10)

| Кейс | Тип | TP | Neg rej | Unknown | Вердикт |
|---|---|---|---|---|---|
| 1 positive (motorcycle→bike) | recall | 1.0 | 1.0 | 0 | ✅ |
| 2 positive (scrubs→nurse→Rich) | recall | 1.0 | 1.0 | 1 (p00002 — см. §5) | ⚠️→✅ |
| 3 negative (два разных bike) | FP | — | 1.0 | 0 | ✅ |
| 4 negative (два nurse) | FP | — | 1.0 | 0 | ✅ |
| 5 negative (generic role poisoned) | FP | — | **0.0** | 0 | ❌ (не блокер) |
| 6 negative (термин=2 объекта) | FP | — | 1.0 | 0 | ✅ |
| 7 provenance (poisoned gender) | poisoned | — | 1.0 | 0 | ✅ |
| 8 provenance (same_entity не доказан) | false validation | — | **0.0** | 1 | ❌ (фикс §5) |

Mock-прогон (0 Qwen) доказывает: харнесс работает, метрики считаются,
чанки идентичны. Реальные цифры — таблица выше.

## 5. Decision gate (§9.2) — рекомендация (владелец + архитектор 2026-08-10)

**РЕЗУЛЬТАТ: entity-context в production — ДА, с ограничением** (конспект
§9.5.3):

1. **Verified-факты (anchor/alias) рендерятся в аудит-промпт** — recall-выгода
   подтверждена (p00035/p00240, кейсы 1/2);
2. **Candidate-relations (same_entity) НЕ рендерятся в аудит-промпт** — кейс 8:
   Qwen принял rendered candidate за факт (changed_fact FP). Остаются для hard
   filters (TIER_B) и repair. **Реализовано (Fix 2):** `render_entity_context_text`
   и production `render_entity_context_block` фильтруют `status=candidate`;
3. **Generic-роли не выдаются** — обеспечено extractor'ом (кейс 5 — не блокер);
4. **B3**: `entity_context_enabled=true`, контекст = только verified.

**Кейс 2 (Fix 3):** p00002 «Медсестра» — НЕ unknown, а РЕАЛЬНЫЙ TP: в этом
синтетическом кейсе nurse = Rich (male) (контекст кейса), поэтому женский
«медсестра подала» — настоящий invented_gender (реальная «The Nurse» главы
0001 — female generic, НЕ Rich — к синтетическому кейсу неприменима).
Gold дополнен: (p00002, invented_gender) + (p00004, invented_gender).

## 6. Состав артефактов

| Артефакт | Путь |
|---|---|
| Харнесс + 8 фикстур + метрики + CLI | `pact_v4/audit/b13_ab.py` |
| Контракт-тесты (mock, 0 Qwen) | `tests/pact_v4/audit/test_b13_ab.py` |
| Source-карта главы 0001 (для owner-run) | генерируется (§3.1), не коммитится |
| Mock-результаты (валидация харнесса) | `_b13_out/` (ab_mock.json, metrics_mock.json, cases_summary.json) — после mock-прогона |
| Реальные результаты | `_b13_out_real/` (после §3) |
