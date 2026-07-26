# CLAUDE_TEST_PLAN_RU

Набор малых оффлайн-тестов для v3.1.1. Приоритет — воспроизведение найденных дефектов **без модели**: почти все находки касаются чистой логики и проверяются на фикстурах за секунды.

Общие соглашения:
- корень тестов — `D:\pact\pact_translator_v3\tests_v31`;
- фикстуры лежат в `tests_v31\fixtures\<test_id>\`;
- модули берутся из `pact_full_pipeline_runner_v1`, поэтому все команды предполагают `sys.path` с этой директорией (см. T-000);
- «требует модель» = нужен запущенный `llama-server`.

Сводка:

| ID | Ловит | Модель |
|---|---|---|
| T-000 | инфраструктура тестов | нет |
| T-001 | CLD-001 | нет |
| T-002 | CLD-001 (маршрутизация) | нет |
| T-003 | CLD-002 | нет |
| T-004 | CLD-002 (согласованность списков) | нет |
| T-005 | CLD-003 | нет |
| T-006 | CLD-009 | нет |
| T-007 | CLD-006 | нет |
| T-008 | CLD-006 (провал батча) | нет |
| T-009 | CLD-008 | нет |
| T-010 | CLD-005 | нет |
| T-011 | CLD-012 | нет |
| T-012 | CLD-011 | нет |
| T-013 | CLD-014 | нет |
| T-014 | CLD-007 | нет |
| T-015 | CLD-013 | нет |
| T-016 | CLD-004 | нет |
| T-017 | CLD-018 | нет |
| T-018 | CLD-017 | нет |
| T-019 | регресс lifecycle (p00026/p00034) | нет |
| T-020 | сквозной smoke ансамбля | да |

---

## T-000. Инфраструктура: общий conftest

**Зачем.** Все v31-модули импортируются друг из друга по имени (`from v31_common import …`) и рассчитывают, что лежат в одной директории. `self_test_v31.py` решает это через `sys.path.insert(0, HERE)`, что привязывает тесты к раскладке. Нужен явный, переносимый способ.

**Фикстуры.** Нет.

**Файл `tests_v31\conftest.py`:**
```python
import os, sys
from pathlib import Path
RUNNER = Path(os.environ.get("PACT_RUNNER_DIR",
    r"D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1"))
PROJECT = Path(os.environ.get("PACT_PROJECT_ROOT", r"D:\pact\pact_translator_v3"))
sys.path.insert(0, str(RUNNER))
```

**Команда:**
```powershell
cd D:\pact\pact_translator_v3\tests_v31
py -m pytest -q
```

**Ожидаемый результат.** Коллекция тестов проходит без ImportError.

**Модель.** Нет.

---

## T-001. Слияние issue разных категорий (CLD-001)

**Зачем.** Зафиксировать, что пересечение спанов само по себе не является основанием для слияния.

**Фикстуры.** Нет, объекты строятся в коде.

**Тест:**
```python
from v31_common import issue_record, merge_duplicate_issues

def test_different_categories_not_merged():
    qwen = issue_record(pid="p00001", category="idiom", problem="wrong idiom",
                        detector="qwen_semantic_primary",
                        target_span="трудно отложить", confidence="high")
    gemma = issue_record(pid="p00001", category="collocation",
                         problem="broken collocation",
                         detector="gemma_russian_primary",
                         target_span="его трудно отложить в сторону",
                         confidence="high")
    merged = merge_duplicate_issues([qwen, gemma])
    assert len(merged) == 2, "разные категории не должны сливаться по подстроке"

def test_same_category_same_span_merged():
    a = issue_record(pid="p00001", category="idiom", problem="x",
                     detector="qwen_semantic_primary",
                     target_span="трудно отложить", confidence="high")
    b = issue_record(pid="p00001", category="idiom", problem="y",
                     detector="gemma_semantic_primary",
                     target_span="трудно отложить", confidence="high")
    merged = merge_duplicate_issues([a, b])
    assert len(merged) == 1 and merged[0]["agreement_count"] == 2
```

**Ожидаемый результат.** До патча первый тест падает (`len == 1`), второй проходит. После патча CLD-001 проходят оба.

**Внимание.** Существующий `self_test_v31.py` содержит утверждение, закрепляющее старое поведение (слияние `meaning` + `calque` по одинаковому спану в одну запись). Его надо привести в соответствие с новым правилом, иначе набор тестов начнёт противоречить сам себе.

**Модель.** Нет.

---

## T-002. Маршрутизация не выдаёт преверификацию за согласие (CLD-001)

**Зачем.** Проверить второй guard: даже при слиянии `independent_detector_agreement` допустим только при точном совпадении отпечатка.

**Фикстуры.** `fixtures\T-002\` — минимальные `manifest.json` (2 блока), `chapter_bible.json` (`{}`), `draft_translations.json`, `v31\primary\{qwen_semantic,gemma_semantic,gemma_russian,gemma_discourse}.json` с `coverage.ok=true`, `completed=2` и подготовленными issues.

**Команда:**
```powershell
py v31_merge_issues.py --project-root D:\pact\pact_translator_v3 `
  --config fixtures\T-002\config.json --start 1 --end 1 --pass-name primary
```

**Ожидаемый результат.** В `merged_issues.json` для issue, собранного из перекрывающихся, но разных по категории замечаний, `verification_route == "dual_cross_judge"`, и он присутствует в обеих очередях. `preverified` содержит только issues с точным совпадением отпечатка.

**Модель.** Нет.

---

## T-003. Детектор mixed_script (CLD-002)

**Зачем.** Главная дыра в детерминированных проверках; прямо соответствует регрессии `p00091`.

**Фикстуры.** Нет.

**Тест:**
```python
def test_mixed_script_detected(runtime):
    block = runtime.Block(pid="p00001", index=0, tag="p", source_html="<p>x</p>",
                          source_text="Mary nodded slowly.", word_count=3,
                          digits=[], inline_spans=[])
    cfg = runtime.merge(runtime.DEFAULTS, {})
    issues = runtime.deterministic_issues(
        [block], {"p00001": "Mary кивнула медленно."}, cfg,
        runtime.Glossary(cfg), {}, runtime.BookBible(Path("book.json")))
    assert any(i.category == "mixed_script" for i in issues)

def test_clean_translation_has_no_mixed_script(runtime):
    ...  # target="Мэри кивнула медленно." → ожидать отсутствие mixed_script
```

**Ожидаемый результат.** До патча первый тест падает. После — проходит, второй остаётся зелёным (контроль ложных срабатываний).

**Дополнительно.** Отдельный кейс на whitelist: токен из `deterministic_qa.mixed_script_allow` не должен порождать issue.

**Модель.** Нет.

---

## T-004. Согласованность списков категорий (CLD-002)

**Зачем.** Структурный тест, который поймает **любую** будущую рассинхронизацию между тем, что детектор умеет порождать, и тем, что объявлено блокирующим. Это ценнее конкретной проверки на `mixed_script`.

**Тест:**
```python
import re
from pathlib import Path
import v31_merge_issues, v31_deterministic_gate, v31_finalize_quality

def test_declared_categories_are_producible(conftest_paths):
    source = (conftest_paths.PROJECT / "pact_translate_v3.py").read_text(encoding="utf-8")
    produced = set(re.findall(r'category="([a-z_]+)"', source))
    declared = (v31_merge_issues.HARD_DETERMINISTIC
                | v31_deterministic_gate.HARD_CATEGORIES
                | v31_finalize_quality.DEFAULT_FAIL_CATEGORIES)
    assert declared <= produced, f"объявлены, но не порождаются: {sorted(declared - produced)}"
```

**Ожидаемый результат.** До патча: падение со списком `['mixed_script']`. После — проходит.

**Модель.** Нет.

---

## T-005. Консолидация вердиктов и uncertain (CLD-003)

**Зачем.** Зафиксировать полную таблицу решений, а не один случай.

**Фикстуры.** `fixtures\T-005\` — `merged_issues.json` с четырьмя issue на маршруте `dual_cross_judge` и парой `cross_verify_{qwen,gemma}.json` с комбинациями вердиктов.

**Матрица кейсов:**

| Qwen | Gemma | Ожидание после патча |
|---|---|---|
| repair/high | repair/high | verified, confidence=high |
| repair/high | repair/medium | verified, confidence=medium |
| keep/high | keep/medium | rejected, confidence=medium |
| repair/high | keep/high | uncertain → поведение по `uncertain_policy` |

**Команда:**
```powershell
py v31_finalize_verification.py --project-root D:\pact\pact_translator_v3 `
  --config fixtures\T-005\config.json --start 1 --end 1 --pass-name primary
```

**Ожидаемый результат.** До патча: `RuntimeError` на первом же кейсе с `medium`, exit ≠ 0. После патча: exit 0, содержимое `verified_issues.json` и `rejected_issues.json` соответствует таблице; при `uncertain_policy=fail` последний кейс по-прежнему валит стадию.

**Модель.** Нет.

---

## T-006. Жёсткий инвариант не отклоняется одним судьёй (CLD-009)

**Фикстуры.** `fixtures\T-006\` — аудиты, где `missing` найден и детерминированной проверкой, и `gemma_semantic`.

**Команда:** `v31_merge_issues.py` как в T-002.

**Ожидаемый результат.** `verification_route == "hard_deterministic"`, issue в `preverified`, очереди судей его не содержат.

**Модель.** Нет.

---

## T-007. Formatting incidents блокируют финализацию (CLD-006)

**Тест:**
```python
def test_incidents_block_integrity(runtime):
    cfg = runtime.merge(runtime.DEFAULTS, {"formatting": {"max_incidents": 0}})
    block = runtime.Block(pid="p00001", index=0, tag="p", source_html="<p><em>x</em></p>",
                          source_text="x", word_count=1, digits=[], inline_spans=[])
    result = runtime.final_integrity(
        "<html><body><p>текст</p></body></html>", [block],
        {"p00001": "текст"}, [{"pid": "p00001", "span_id": "s1"}], cfg)
    assert result["ok"] is False
```

**Ожидаемый результат.** До патча `ok is True` (инцидент попадает в `warnings`). После — `False`.

**Дополнительный кейс.** `max_incidents=5` при одном инциденте → `ok is True`; проверяет, что порог настраиваемый.

**Модель.** Нет.

---

## T-008. Провал батча форматирования порождает инциденты (CLD-006)

**Зачем.** Сейчас полный провал батча не порождает **ни одного** инцидента, поэтому T-007 его не поймает — дыра остаётся невидимой.

**Тест.** Заглушка `ApiClient`, всегда возвращающая невалидный JSON; вызвать `run_formatting` на блоке с `inline_spans` при `formatting.required=False`.

**Ожидаемый результат.** После патча `incidents` непуст и содержит запись с `reason == "batch_failed"` для каждого PID батча. До патча — пустой список.

**Модель.** Нет (заглушка вместо клиента).

---

## T-009. changed_pids не реагирует на пробелы (CLD-008)

**Фикстуры.** `fixtures\T-009\` — `draft_translations.json` со значением `"Он  ушёл."` (два пробела) и `v31_final_translations.json` со значением `"Он ушёл."`, плюс полный набор артефактов, необходимый финальному gate для прохождения.

**Команда:**
```powershell
py v31_finalize_quality.py --project-root D:\pact\pact_translator_v3 `
  --config fixtures\T-009\config.json --start 1 --end 1
```

**Ожидаемый результат.** `v31_quality_gate.json → changed_pids == []` и `post_repair_report.json → changed_candidates == 0`. До патча — 1.

**Модель.** Нет.

---

## T-010. Неаудированные PID после residual repair видны в отчёте (CLD-005)

**Фикстуры.** Как T-009, но `v31_primary_translations.json` и `v31_final_translations.json` различаются одним PID **по существу**, а не по пробелам.

**Ожидаемый результат.** После патча в `v31_quality_gate.json → coverage` присутствует ключ `residual_post_repair_unaudited_pids` со списком из одного PID. Финализация не блокируется (вариант 1 исправления).

**Модель.** Нет.

---

## T-011. post_repair_report отражает реальный lifecycle (CLD-012)

**Фикстуры.** `fixtures\T-011\` — `lifecycle.json` с одной записью в статусе `retry_required`.

**Ожидаемый результат.** Два уровня:
1. `v31_finalize_quality.py` должен упасть раньше (проверка `unresolved_lifecycle`) — это подтверждает, что настоящий барьер работает;
2. если искусственно обойти первый gate и подсунуть `post_repair_report.json` с ненулевым `unresolved_total`, то `pact_translate_v3.py --phase finalize` должен бросить `PipelineError` — это подтверждает, что legacy-проверка перестала быть заглушкой.

**Модель.** Нет.

---

## T-012. Идемпотентность ledger (CLD-011)

**Фикстуры.** `fixtures\T-012\` — готовый `source_scene_map.json` и `book_consistency_ledger.json` с одной парой обращений.

**Команда (дважды подряд):**
```powershell
py v31_source_analysis.py --project-root D:\pact\pact_translator_v3 `
  --config fixtures\T-012\config.json --start 1 --end 1
```

**Ожидаемый результат.** Длина `history` каждого ключа в `book_consistency_ledger.json` одинакова после первого и второго запуска. До патча растёт на 1 за запуск.

**Модель.** Нет (стадия целиком переиспользует кеш и модель не вызывает — в этом и смысл теста).

---

## T-013. Устойчивость к повреждённому кешу (CLD-014)

**Фикстуры.** `fixtures\T-013\` — корректный набор для одного раунда repair, где `v31\primary\repairs\round_01\p00001.json` содержит `{}`.

**Ожидаемый результат.** После патча стадия пересчитывает запись (или помечает кеш невалидным по `version`) и завершается с exit 0. До патча — `KeyError: 'pid'` на следующей стадии.

**Дополнительный кейс.** Запись с `"version": "3.0.0"` должна инвалидироваться так же.

**Модель.** Нет, если пересчёт замокан; иначе да. Рекомендую замокать `complete_json`.

---

## T-014. Эквивалентность выбора глав (CLD-007)

**Фикстуры.** Временный каталог с пустыми `c_2.html`, `c_10.html`, `c_60.html`, `c_100.html`.

**Тест (Python-сторона):**
```python
def test_python_selection_order(runtime, tmp_path):
    cfg = runtime.merge(runtime.DEFAULTS, {"paths": {"input_dir": str(tmp_path)}})
    names = [p.name for p in runtime.select_files(cfg, 1, 4)]
    assert names == ["c_2.html", "c_10.html", "c_60.html", "c_100.html"]
```

**PowerShell-сторона:**
```powershell
$ps = @(Get-ChildItem $dir -Filter *.html -File | Sort-Object Name | ForEach-Object Name)
$py = py -c "import json,sys; ..."   # напечатать список из select_files
if (Compare-Object $ps $py) { throw 'Порядок глав расходится между PS и Python' }
```

**Ожидаемый результат.** До патча `Compare-Object` даёт различия. После — списки совпадают.

**Модель.** Нет.

---

## T-015. Порог английского остатка развязан (CLD-013)

**Тест:**
```python
def test_repair_threshold_independent(runtime):
    block = runtime.Block(pid="p00001", index=0, tag="p", source_html="<p>x</p>",
                          source_text="He read the Necronomicon aloud.", word_count=5,
                          digits=[], inline_spans=[])
    cfg = runtime.merge(runtime.DEFAULTS, {"validation": {
        "english_sequence_min_words": 2, "repair_english_min_words": 4}})
    errors = runtime.validate_single_repair(
        "p00001", "Он прочитал Necronomicon вслух.", {"p00001": block}, cfg, "старый текст")
    assert not any("English residue" in e for e in errors)
```

**Ожидаемый результат.** После патча ошибок про английский нет; при `repair_english_min_words=2` — есть.

**Модель.** Нет.

---

## T-016. Инвалидация при -RedoSourceAnalysis (CLD-004)

**Фикстуры.** Мини-run: `work\<stem>\drafts\c1.json`, `draft_translations.json`, `v31\primary\merged_issues.json`.

**Команда:**
```powershell
$before = (Get-FileHash work\<stem>\draft_translations.json).Hash
.\run_full_pipeline_v31.ps1 -Start 1 -End 1 -RedoSourceAnalysis -SkipPreflight
```

**Ожидаемый результат.** После патча одно из двух: либо скрипт падает с явным требованием `-RedoTranslation`, либо `work\<stem>\drafts` удалён и `v31` отсутствует к моменту перевода. До патча — хеш и `v31` не изменяются.

**Модель.** Да, если выбран вариант с фактическим перепереводом. Вариант с `throw` проверяется без модели — рекомендую начать с него.

---

## T-017. Разбор timing-строк preflight (CLD-018)

**Фикстуры.** Сохранённый образец `server_logs\GemmaTranslate_*_stderr.log` из текущего прогона (единственная фикстура, которую нужно взять из реального run).

**Тест (PowerShell):**
```powershell
$logText = Get-Content -LiteralPath $sample -Raw
$gen = [regex]::Matches($logText, '(?m)^.*?\|\s+eval time\s*=\s*[\d.]+\s*ms\s*/\s*\d+\s*tokens.*?([\d.]+)\s*tokens per second')
$alt = @($logText -split "`n" | Where-Object { $_ -match '\beval time\s*=' -and $_ -notmatch 'prompt eval time' })
if ($gen.Count -eq 0) { throw 'Текущий regex preflight не находит generation timing' }
```

**Ожидаемый результат.** Оба подхода находят одинаковое число совпадений и одинаковое последнее значение. Расхождение означает, что нужно переходить на вариант монитора.

**Модель.** Нет, но нужен образец лога из реального прогона (QST-003).

---

## T-018. Однократный отказ token endpoint (CLD-017)

**Тест.** Подменить `ApiClient._post` заглушкой, считающей вызовы и всегда бросающей исключение; вызвать `token_count` трижды.

**Ожидаемый результат.** После патча счётчик сетевых вызовов равен числу попыток **одного** обращения, а не троекратному; второй и третий вызовы возвращают оценку мгновенно.

**Модель.** Нет.

---

## T-019. Регрессии lifecycle из handoff

**Зачем.** Раздел 8 handoff перечисляет PID, по которым уже были ошибки жизненного цикла. Часть из них проверяема оффлайн, без обращения к тексту главы.

| Регрессия | Проверка | Как |
|---|---|---|
| `p00034`: `replace` без изменения текста | `validate_single_repair` возвращает `"unchanged"` | прямой вызов с идентичными `candidate` и `current_text` |
| `p00026`: отклонённый repair запускает retry | `v31_adjudicate` при всех `passed=false` формирует `retry_requests` и `outcome == "retry_required"`, а `translations[pid]` не меняется | фикстура с четырьмя отрицательными gate |
| `p00164`, `p00398`: «оставить как есть» не становится repair issue | связано с CLD-001; покрыто T-001/T-002 | — |
| `keep` не считается исправлением | `semantic_accept`/`russian_accept` возвращают `False` при `verdict="keep"` | прямой вызов |

**Ожидаемый результат.** Все проходят на текущем коде — это тесты-«замки», фиксирующие уже исправленное поведение, чтобы будущие патчи его не сломали.

**Модель.** Нет.

---

## T-020. Сквозной smoke ансамбля

**Зачем.** Единственный тест, требующий моделей. Нужен для проверки, что после патчей 1.1–1.4 конвейер вообще проходит целиком.

**Фикстуры.** Синтетическая мини-глава: HTML из 6 абзацев, в одном из которых намеренно оставлен латинский токен, в другом — искажена модальность.

**Команда:**
```powershell
.\run_full_pipeline_v31.ps1 -ProjectRoot <тестовый корень> -Start 1 -End 1 -Reset
```

**Ожидаемый результат.**
1. `state.json → status == "complete"`;
2. `v31_quality_gate.json → ok == true`;
3. латинский токен исправлен, и в `issue_lifecycle.json` есть запись категории `mixed_script` со статусом `resolved_repair`;
4. `merged_issues.json` не содержит маршрутов `independent_detector_agreement`, полученных слиянием разных категорий;
5. время прогона фиксируется как базовая линия — при подтверждении CLD-017 оно должно заметно упасть после исправления.

**Модель.** Да, полный цикл всех четырёх профилей.

---

## Чего этот план не покрывает

Осознанно оставлено за рамками, поскольку требует данных прогона, а не фикстур:

- качество перевода как таковое (regression PID `p00062`, `p00152`, `p00250`, `p00273`, `p00285–p00286`, `p00343` из handoff проверяются только чтением текста);
- сквозная согласованность `ты/вы` по сцене — оценивается человеком или отдельным discourse-прогоном;
- калибровка порогов `max_incidents`, `mixed_script_allow`, `uncertain_policy` — задаётся по результатам первого прогона после патчей (QST-004, QST-006, QST-007);
- производительность профилей моделей — по-прежнему измеряется preflight-ом и не должна меняться на основании этих тестов.
