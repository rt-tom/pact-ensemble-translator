# CLAUDE_PIPELINE_AUDIT_RU

Независимый статический аудит Pact Ensemble Translator v3.1.1.
Дата: 25 июля 2026. Источник: `02_CURRENT_CODE` + `config.full_pipeline.v31.snapshot.json`.
Результаты runs не анализировались (намеренно не включены в пакет).

---

## 1. Executive summary

Архитектура ансамбля реализована в основном добросовестно: роли разделены, взаимная проверка есть, булевы поля gate валидируются строго (`strict_bool`), кандидаты проходят четыре независимых барьера, финальный gate пересчитывает детерминированные проверки на итоговом тексте. Большинство lifecycle-проблем, перечисленных в разделе 7 handoff, в коде действительно закрыто: `keep` не считается исправлением, `replace` без изменения текста отклоняется (`validate_single_repair` → `"unchanged"`), отклонённый repair уходит в retry, а не возвращает draft.

Однако найдены дефекты, которые прямо противоречат заявленным целям.

Три из них я считаю блокирующими для запуска всей книги:

1. **CLD-001** — механизм дедупликации создаёт ложное «независимое согласие двух семейств моделей» на основании пересечения строк, а не совпадения проблемы. Такие issues обходят cross-verification полностью. Это подрывает главный архитектурный принцип проекта.
2. **CLD-002** — категория `mixed_script` присутствует во всех трёх списках блокирующих категорий, но **не порождается ни одной проверкой**. Одиночный латинский токен внутри русского предложения (ровно случай `Mary` из regression-списка, PID `p00091`) не ловится ни `english_residue`, ни `entity_consistency`, ни чем-либо ещё.
3. **CLD-003** — `fail_on_uncertain=true` превращает штатное расхождение двух судей в необратимую остановку всего прогона после того, как оплачены все аудиты. Для автоматического перевода книги это гарантированный стопор.

Отдельно отмечу класс проблем «формальный JSON success при фактической ошибке текста», которому задание просило уделить особое внимание: **CLD-006** (потерянные inline-спаны — только warning, `state.json=complete`), **CLD-012** (legacy post-repair gate физически не может сработать, так как читает захардкоженный ноль), **CLD-005** (правки residual-прохода никогда не проходят повторный аудит уровня главы, но покрытие в quality gate записывается как полное).

Не найдено: ошибок в accounting-инвариантах финального gate (они плотные и корректные), ошибок маршрутизации судей по семействам, проблем с рекурсивным делением батчей, нарушений atomic write в `v31_common`.

**Рекомендация:** не запускать книгу до исправления CLD-001, CLD-002, CLD-003. Остальное можно чинить итеративно.

---

## 2. Фактическая архитектура

Подробная карта — в `CLAUDE_ARCHITECTURE_MAP_RU.md`. Здесь только выводы, важные для находок.

**Что действительно активно.** Все 13 файлов из `$RequiredRunnerFiles` вызываются. `04_LEGACY_REFERENCE` не вызывается ниоткуда — проверено grep по активному call graph. Legacy-секции `audit.enabled=false` и `repair.enabled=false` выключают старые стадии в `pact_translate_v3.py`, но сам файл остаётся активным как runtime-библиотека: `load_runtime()` импортирует его в каждом v31-модуле ради `deterministic_issues`, `validate_single_repair`, `Glossary`, `BookBible`, `ApiClient`, `blocks_from_manifest`. Поэтому дефекты в этом файле — **active path**, несмотря на выключенные фазы.

**Что выглядит включённым, но не действует.**
- `ensemble_v31.repair.alternative_for_multiple_issues` читается через `stage.get("alternative_categories", …)`, но сам флаг `alternative_for_multiple_issues` **нигде не проверяется** — `difficult` вычисляется по четырём другим признакам (`v31_repair.py:44-50`). Выключить генерацию двух кандидатов через конфиг невозможно.
- `post_repair_verifier.reject_policy`, `uncertain_policy`, `accept_confidences`, `max_repair_rounds` в snapshot — от legacy-верификатора; v3.1 их не читает (`max_repair_rounds` берётся из `ensemble_v31`).
- `validation.english_residue_is_error=false` влияет только на chunk-валидацию перевода (`pact_translate_v3.py:1254`), но **не** на детерминированный детектор (`:1763-1774`), который всё равно порождает `english_residue` с severity `critical`. Это, вероятно, не осознаётся: флаг выглядит как «отключить проверку», а она остаётся блокирующей в финальном gate.
- Секция `verifier` в snapshot не используется v3.1 вообще.

**Расхождение snapshot и кода.** Snapshot содержит только те ключи, которые пишет runner; всё остальное (`chunking`, `deterministic_qa`, `html`, `style`, `validation.strict_digits`, `min/max_length_ratio`, `timeout_seconds`, `http_retries`) берётся из `DEFAULTS` внутри Python. То есть **snapshot не является полным описанием фактической конфигурации** и не должен использоваться как единственный источник при разборе прогона.

---

## 3. Confirmed defects

### CLD-001 — ложное «независимое согласие» из-за merge по подстроке

- **severity:** BLOCKER · **status:** CONFIRMED · **active path:** YES
- **category:** merge
- **файлы:** `v31_common.py` → `merge_duplicate_issues` (строки 335-376), `issue_fingerprint` (325-332); `v31_merge_issues.py` → `main`, блок маршрутизации (103-116)

**Механизм.** В `merge_duplicate_issues` два issue одного PID сливаются, если выполняется одно из двух:
```python
if issue_fingerprint(existing) == fp: chosen = existing
...
a = fold(issue.get("target_span")); b = fold(existing.get("target_span"))
if a and b and (a == b or (len(a) > 8 and (a in b or b in a))):
    chosen = existing
```
Вторая ветка **полностью игнорирует категорию и суть проблемы**. Достаточно, чтобы Qwen и Gemma указали пересекающиеся русские фрагменты длиннее 8 символов. Реалистично это происходит постоянно: аудиторы цитируют куски одного и того же предложения.

Дальше в `v31_merge_issues.py` слитый issue получает `detector_families = {"qwen", "gemma"}`, и если оба исходных замечания имели `confidence=high`, срабатывает:
```python
if qwen_high and gemma_high:
    issue.update({"verification_route": "independent_detector_agreement",
                  "verification_decision": "repair", "verification_confidence": "high", ...})
    preverified.append(issue)
```
Issue уходит в `verified_issues.json` **без единого обращения к судье**.

Вторая часть дефекта: при слиянии выживает только по одному значению каждого поля — побеждает самое длинное (`v31_common.py:370-372`). Категория и `problem` берутся от первого попавшего в группу (порядок — сортировка по `(pid_num, category)`). То есть если Qwen нашёл смысловую ошибку, а Gemma — грамматическую в перекрывающемся фрагменте, наружу выйдет **одна** issue с одной категорией и одним `required_invariant`; вторая инструкция сохранится только внутри `source_issues` и не попадёт ни в `repair_instruction`, ни в проверку `all_issues_fixed`.

**Trigger scenario.** Qwen: `target_span="трудно отложить"`, category=`idiom`, confidence=high. Gemma Russian: `target_span="его трудно отложить в сторону"`, category=`collocation`, confidence=high. `len(a)=15 > 8`, `a in b` → слияние → преверификация → repair без судьи. Обратный случай столь же вероятен: обе модели «уверенно» ошибаются на корректном тексте в перекрывающихся спанах — и правка вносится в хороший перевод без единой проверки. Ровно то, от чего защищают PID `p00164` и `p00398` из regression-списка (`оставить как есть` не должно становиться repair issue).

**Impact.** Разрушен центральный принцип проекта («ни одна модель не должна быть единственным источником обнаружения и одновременно судьёй»). Ложные срабатывания попадают в repair напрямую; настоящие парные ошибки теряют половину инструкции. Чем чувствительнее аудиты, тем чаще срабатывает.

**Минимальное исправление.** Два независимых guard, оба локальные:
1. В `merge_duplicate_issues` во второй ветке добавить условие совместимости категорий — сливать по подстроке только при `fold(issue["category"]) == fold(existing["category"])`, либо при принадлежности к одной группе (задать явный dict `CATEGORY_FAMILY`). Для остальных случаев оставить их отдельными issue.
2. В `v31_merge_issues.py` в ветке `qwen_high and gemma_high` требовать, чтобы слияние произошло по **точному** fingerprint. Технически: в `merge_duplicate_issues` проставлять `chosen["merge_reason"] = "fingerprint" | "span_overlap"`, и допускать `independent_detector_agreement` только при `merge_reason == "fingerprint"`; иначе → `dual_cross_judge`.
3. Дополнительно (по желанию): агрегировать `required_invariant` всех `source_issues` в список `required_invariants`, чтобы repair видел все условия.

**Regression test.** Оффлайн, без модели: собрать два `issue_record` с пересекающимися, но разными `target_span` и разными категориями; убедиться, что `merge_duplicate_issues` вернёт 2 записи, а не 1; затем прогнать логику маршрутизации и убедиться, что route ≠ `independent_detector_agreement`.

**Побочные эффекты.** Вырастет число issue, уходящих в cross-verification → дольше прогон и больше шансов получить `uncertain` (см. CLD-003, чинить вместе). Число дублей в repair-промпте может немного вырасти; это компенсируется тем, что группировка в `v31_repair.py` идёт по PID, а не по issue.

---

### CLD-002 — `mixed_script` объявлен блокирующим, но не детектируется

- **severity:** BLOCKER · **status:** CONFIRMED · **active path:** YES
- **category:** quality
- **файлы:** `v31_merge_issues.py:17` (`HARD_DETERMINISTIC`), `v31_deterministic_gate.py:17` (`HARD_CATEGORIES`), `v31_finalize_quality.py:16-19` (`DEFAULT_FAIL_CATEGORIES`), `run_full_pipeline_v31.ps1:152`; отсутствующий детектор — `pact_translate_v3.py` → `deterministic_issues` (1682-1868)

**Механизм.** `deterministic_issues` порождает ровно девять категорий: `missing`, `number`, `number_word`, `tone_profanity`, `english_residue`, `length_outlier`, `name_consistency`, `entity_consistency`, `narrator_gender`. Строка `mixed_script` не встречается в `pact_translate_v3.py` вообще (проверено grep по всему файлу). Значит:
- в `HARD_DETERMINISTIC` реально работает только `missing`;
- в `HARD_CATEGORIES` и `fail_deterministic_categories` `mixed_script` — мёртвый элемент.

Дыру не закрывает и `english_residue`, потому что `detect_english_sentence` требует **одновременно**:
```python
if len(latin) >= minimum and len(latin) >= max(1, len(cyrillic) * 2):
```
При `english_sequence_min_words=2` нужно ≥2 латинских слов **и** чтобы латиница вдвое превосходила кириллицу в предложении. Внутри нормального русского предложения одиночная латинская лексема (`Mary`, `Rose`, `Hyena`) даёт `len(latin)=1` → не срабатывает. Даже два латинских слова подряд в русском предложении из 10 слов не пройдут второе условие.

`entity_consistency` тоже не спасает: он требует, чтобы термин был в глоссарии, чтобы `source_term_present` нашёл английский оригинал в исходнике и чтобы `expected_core[:1].isupper()`. Латинский остаток от термина, которого нет в глоссарии, невидим.

**Trigger scenario.** PID `p00091` из regression-списка: русское предложение с оставшимся `Mary` вместо `Мэри`. Ни один детерминированный детектор не срабатывает; если модельные аудиты это пропустят (а `gemma_russian` не видит английский и может принять имя за допустимую транслитерацию), глава получит `ok: true` от финального gate.

**Impact.** Заявленный «жёсткий инвариант» отсутствует. Финальный quality gate декларирует защиту от смешения алфавитов, которой нет. Прямо соответствует известной регрессии из handoff.

**Минимальное исправление.** Добавить в `deterministic_issues` проверку и не трогать остальную архитектуру (все три списка категорий уже готовы её принять):
```python
# после блока english_residue
if cfg["deterministic_qa"].get("mixed_script_check", True):
    stray = re.findall(r"\b[A-Za-z][A-Za-z'’\-]*\b", target)
    if stray and not detect_english_sentence(target, minimum):
        issues.append(Issue(
            pid=block.pid, severity="critical", category="mixed_script",
            problem=f"Untranslated Latin token(s): {', '.join(sorted(set(stray))[:5])}",
            repair_instruction="Заменить латинские токены кириллицей по глоссарию.",
            source="deterministic", deterministic=True,
        ))
```
Плюс в `DEFAULTS["deterministic_qa"]` добавить `"mixed_script_check": True`. Нужно предусмотреть whitelist для намеренной латиницы (если в тексте она есть) — вынести в `deterministic_qa.mixed_script_allow` список разрешённых токенов.

**Regression test.** Оффлайн: вызвать `deterministic_issues` на блоке с `source_text="Mary nodded."` и `target="Mary кивнула."` — ожидать issue с категорией `mixed_script`. Контроль: `target="Мэри кивнула."` — ожидать пусто.

**Побочные эффекты.** Возможны ложные срабатывания на намеренной латинице (названия, магические формулы). Поэтому whitelist обязателен, а первый прогон стоит сделать с `mixed_script` **вне** `fail_deterministic_categories`, чтобы измерить шум, и только потом включить как блокирующую.

---

### CLD-003 — расхождение судей необратимо убивает прогон

- **severity:** HIGH · **status:** CONFIRMED · **active path:** YES
- **category:** repair (verification)
- **файлы:** `v31_finalize_verification.py:88-93, 138-143, 158-159`; `run_full_pipeline_v31.ps1:150`

**Механизм.** Для `dual_cross_judge` требуется **точное** совпадение пары:
```python
if q_pair == ("repair","high") and g_pair == ("repair","high"): decision = "repair","high"
elif q_pair == ("keep","high") and g_pair == ("keep","high"): decision = "keep","high"
else: decision, confidence = "uncertain", "medium"
```
Любая иная комбинация — включая `("repair","high") + ("repair","medium")`, то есть **полное согласие по существу** при разной уверенности — даёт `uncertain`. Для одиночных судей `uncertain` возникает и при `repair/medium`, и при `keep/medium`.

Затем:
```python
if uncertain and cfg[...]["verification"].get("fail_on_uncertain", True):
    raise RuntimeError(f"{len(uncertain)} issue(s) remain uncertain …")
```
Исключение → ненулевой exit code → `Invoke-PythonStage` бросает → весь run падает. Артефакты сохраняются, но восстановления нет: при повторном запуске `merged_issues.json` и кеши cross-verify будут переиспользованы (нет `--force`), судьи детерминированы (`temperature=0`) → **тот же самый `uncertain`, тот же самый обвал**. Единственный выход — ручное вмешательство: `-RedoQuality` (полный пересчёт всех аудитов) или правка JSON руками.

**Trigger scenario.** Любая спорная стилистическая правка, где Gemma говорит `repair/high`, а Qwen `repair/medium`. Вероятность на главу в 400 PID при десятках issue — практически единица.

**Impact.** Прямое противоречие цели «участие человека не предполагается ни на одном этапе». Прогон умирает после того, как потрачены все четыре аудита и обе cross-verification. Детерминированность моделей делает падение самовоспроизводящимся.

**Минимальное исправление.** Не менять модели, добавить политику разрешения в существующую секцию `ensemble_v31.verification`:
1. Согласие по решению при разной уверенности трактовать по решению, с понижением confidence:
```python
q_dec, g_dec = q_record.get("decision"), g_record.get("decision")
if q_dec == g_dec and q_dec in {"repair", "keep"}:
    highs = {q_record.get("confidence"), g_record.get("confidence")}
    decision = q_dec
    confidence = "high" if highs == {"high"} else "medium"
else:
    decision, confidence = "uncertain", "medium"
```
2. Добавить `uncertain_policy` со значениями `fail` (текущее), `repair` (консервативно чинить), `keep` (консервативно оставлять). Для автоматического прогона книги разумно `repair`: правку всё равно потом фильтруют четыре post-gate, а `keep` рискует замолчать реальную ошибку.
3. Оставить `fail_on_uncertain=true` только как диагностический режим.

Важно: пункт 1 без пункта 2 недостаточен — одиночные судьи с `medium` всё ещё дают `uncertain`.

**Regression test.** Оффлайн: подложить синтетические `cross_verify_{qwen,gemma}.json` с парой `repair/high` + `repair/medium`, запустить `v31_finalize_verification.py`, ожидать `verified` = 1 и exit 0, а не RuntimeError.

**Побочные эффекты.** При `uncertain_policy=repair` вырастет объём repair-раунда. Гейты его отфильтруют, но прогон удлинится. Стоит логировать долю issue, разрешённых политикой, чтобы видеть, не превратился ли механизм в основной путь.

---

### CLD-004 — `-RedoSourceAnalysis` ничего не переделывает ниже по цепочке

- **severity:** HIGH · **status:** CONFIRMED · **active path:** YES
- **category:** resume
- **файлы:** `run_full_pipeline_v31.ps1:430-447`; `pact_translate_v3.py` → `Runner.translate` (3097-3131)

**Механизм.** При `-RedoSourceAnalysis` без других флагов runner делает ровно две вещи: удаляет `book_consistency_ledger.json` и добавляет `--force` к source analysis. Он **не** вызывает `Remove-QualityArtifacts` и **не** удаляет `drafts/`.

Дальше `Runner.translate` переиспользует перевод по чанкам:
```python
if draft_path.exists():
    saved = read_json(draft_path, {})
    current = saved.get("translations") or {}
    if all(pid in current for pid in chunk.pids):
        accepted.update(...); continue
```
Все чанки уже есть → перевод не пересчитывается. Но `source_scene_map.json` подаётся в перевод именно через `Runner.translate` (`source_scene_map = read_json(work / "source_scene_map.json", {})`) — то есть новый анализ **не влияет ни на один символ перевода**.

Аудиты v31 тоже переиспользуются (`merged_issues.json` и `{mode}.json` существуют, `--force` не передан), хотя `scene_notes_for_pids` и `dialogue_scene_notes` читают уже изменившийся `source_scene_map.json`. Результат: артефакты аудита относятся к старым SOURCE NOTES, а на диске лежат новые.

**Trigger scenario.** Пользователь недоволен качеством source analysis, запускает `-RedoSourceAnalysis`, видит в логах успешную переработку всех батчей, получает `PIPELINE V3.1 COMPLETE` — и ровно тот же перевод и те же issues, что были.

**Impact.** Молчаливый no-op, который выглядит как исправление. Дополнительно нарушается consistency: `source_scene_map.json` и `draft_translations.json` перестают соответствовать друг другу, и никакая проверка этого не фиксирует (в `v31_finalize_quality` нет сверки происхождения).

**Минимальное исправление.** В runner привести инвалидацию в соответствие с зависимостями:
```powershell
if ($RedoSourceAnalysis -and -not $RedoTranslation) {
    throw '-RedoSourceAnalysis requires -RedoTranslation: the draft depends on the scene map.'
}
```
Либо, если нужно мягче, — трактовать `-RedoSourceAnalysis` как включающий `-RedoTranslation`:
```powershell
if ($RedoSourceAnalysis) { $RedoTranslation = $true }
```
поставив это **до** блока инвалидации (строка ~430).

**Regression test.** Скриптовый: создать fixture-run, зафиксировать хеш `draft_translations.json`, запустить с `-RedoSourceAnalysis`, убедиться, что либо стадия отказалась стартовать, либо `drafts/` удалён и хеш пересчитан.

**Побочные эффекты.** `-RedoSourceAnalysis` станет дорогим (полный перевод + аудит). Это честная стоимость операции; дешёвого варианта здесь и не было — он просто не работал.

---

### CLD-005 — правки residual-прохода не проходят повторный аудит главы

- **severity:** HIGH · **status:** CONFIRMED · **active path:** YES
- **category:** coverage
- **файлы:** `run_full_pipeline_v31.ps1:453-459`; `v31_finalize_quality.py:104-128`

**Механизм.** Порядок стадий:
```
Run-AuditPass 'residual' v31_primary_translations.json   ← аудит текста ПОСЛЕ primary repair
Run-RepairPass 'residual' v31_primary_translations.json  ← создаёт v31_final_translations.json
v31_finalize_quality.py                                   ← финальный gate
```
Всё, что изменено в residual repair, проверяется **только** тремя post-gate с контекстом ±2 PID и детерминированным гейтом. Полного аудита главы после последней правки нет. В частности, `gemma_discourse` (окно 30 PID, overlap 10 — единственная проверка сквозного `ты/вы`, смены говорящего и единообразия имён) **никогда не видит финальный текст**.

При этом `v31_finalize_quality` записывает покрытие так:
```python
coverage[f"{pass_name}:{detector}"] = cov
if not path.exists() or not cov.get("ok") or int(cov.get("completed", -1)) != len(expected_pids):
    unresolved.append(...)
```
и кладёт это в `v31_quality_gate.json` как характеристику главы. Формально верно (покрытие residual-аудита действительно полное), фактически вводит в заблуждение: покрытие относится к тексту **до** residual repair.

**Trigger scenario.** Residual repair меняет обращение в PID `p00254`. Локальные gate одобряют (в окне ±2 PID всё согласовано). Но по всей сцене теперь два разных регистра для одной пары. Handoff явно требует проверять «сквозную согласованность `ты/вы` по всей сцене, а не только отдельные PID» — а код этого после последней правки не делает.

**Impact.** Класс ошибок, ради которых заведён discourse-аудит, систематически не ловится в правках последнего прохода. `v31_quality_gate.json` при этом сообщает полное покрытие.

**Минимальное исправление.** Два варианта, оба без редизайна:
1. **Дёшево и честно.** В `v31_finalize_quality.py` посчитать `residual_changed = [pid for pid in expected_pids if primary.get(pid) != translations.get(pid)]` и, если он непуст, дописать в `coverage` явный маркер:
```python
coverage["residual_post_repair_unaudited_pids"] = residual_changed
```
Это не блокирует финализацию, но перестаёт выдавать неполное покрытие за полное и даёт список PID для ручного просмотра.
2. **Строже.** Если `residual_changed` непуст — запустить один дополнительный `gemma_discourse` прогон по `v31_final_translations.json` и потребовать нулевого числа issue. В runner это одна строка после `Run-RepairPass 'residual'`.

Рекомендую сделать (1) немедленно и (2) — перед запуском книги.

**Regression test.** Fixture: `v31_primary_translations.json` и `v31_final_translations.json`, отличающиеся одним PID; запустить `v31_finalize_quality.py`; ожидать непустой `residual_post_repair_unaudited_pids` в `v31_quality_gate.json`.

**Побочные эффекты.** Вариант (2) добавляет один прогон discourse на главу (окна по 30 PID) — заметная, но не катастрофическая стоимость. Есть риск зацикливания, если новый discourse-аудит находит issue: нужно ограничить его одним проходом и при находках падать с явным сообщением, а не запускать ещё один repair-цикл.

---

### CLD-006 — потерянное inline-форматирование проходит как success

- **severity:** HIGH · **status:** CONFIRMED · **active path:** YES
- **category:** formatting
- **файлы:** `pact_translate_v3.py` → `final_integrity` (2874-2908), `run_formatting` (2696-2820), `Runner.finalize` (3228-3278); `run_full_pipeline_v31.ps1:126-130`

**Механизм.** Три уровня деградации, ни один не блокирует:
1. `formatting.required=false` (выставлено runner-ом): при провале всех попыток батча `if not mappings and cfg["formatting"]["required"]: raise` не срабатывает — батч молча отдаёт `html.escape(перевод)`, то есть **весь курсив/полужирный/ссылки в этих PID теряются**;
2. невосстановленные отдельные спаны собираются в `incidents`;
3. в `final_integrity` инциденты попадают только в `warnings`:
```python
if formatting_incidents:
    warnings.append(f"{len(formatting_incidents)} inline spans were not restored.")
return {"ok": not errors, "errors": errors, "warnings": warnings, ...}
```
`ok` зависит **только** от `errors`. Дальше `Runner.finalize` при `ok=true` пишет `state.json = {"status": "complete"}`.

Дополнительно `detect_english_sentence` на финальном HTML тоже даёт лишь warning (строка 2890) — при том, что `english_residue` объявлена блокирующей категорией в `fail_deterministic_categories`. Два места оценивают одно и то же свойство с противоположной строгостью.

**Trigger scenario.** Gemma не справляется с форматированием батча из 12 блоков (по описанию проекта такие срывы уже были — HTTP 500 на структурированных запросах). Батч отдаёт плоский текст. Финальный HTML теряет всю разметку в этих абзацах. `state.json` — `complete`, монитор — `PIPELINE V3.1 COMPLETE`, `v31_quality_gate.json` — `ok: true`.

**Impact.** Именно тот сценарий, который задание выделило особо: JSON-статусы зелёные, текст фактически повреждён. Для книги, где курсив несёт смысловую нагрузку, это ощутимая потеря качества, невидимая в отчётах.

**Минимальное исправление.** Ввести порог, не ломая текущую логику:
```python
# в final_integrity, после подсчёта incidents
limit = int(cfg["formatting"].get("max_incidents", 0))
if formatting_incidents and len(formatting_incidents) > limit:
    errors.append(f"{len(formatting_incidents)} inline spans were not restored (limit {limit}).")
```
и добавить `"max_incidents": 0` в `DEFAULTS["formatting"]`. Плюс отдельно: в `run_formatting` при `not mappings` записывать явный инцидент вида `{"pid": pid, "reason": "batch_failed"}` для каждого PID батча — сейчас полный провал батча не порождает инцидентов вовсе и потому вообще невидим.

**Regression test.** Оффлайн: блок с `inline_spans`, пустой `mappings` → `run_formatting` должен вернуть непустой `incidents`; `final_integrity` с `incidents` длины 1 и `max_incidents=0` → `ok == False`.

**Побочные эффекты.** Прогоны начнут падать на финализации там, где раньше «успешно» завершались. Это желаемое поведение, но первый запуск лучше сделать с `max_incidents` заведомо большим, чтобы измерить реальный уровень инцидентов, а потом опустить порог.

---

### CLD-007 — рассинхронизация выбора глав между PowerShell и Python

- **severity:** HIGH · **status:** CONFIRMED (расхождение кода) / impact — см. QST-001 · **active path:** YES
- **category:** coverage
- **файлы:** `run_full_pipeline_v31.ps1:65-68`; `pact_translate_v3.py:3363-3377`

**Механизм.** PowerShell:
```powershell
$AllInputFiles = @(Get-ChildItem … -Filter '*.html' -File | Sort-Object Name)
$SelectedInputFiles = @($AllInputFiles | Select-Object -Skip ($Start-1) -First ($End-$Start+1))
```
Python:
```python
files = sorted(Path(...).glob("*.html"), key=lambda path: natural_key(path.name))
return files[first - 1:last]
```
`Sort-Object Name` — обычное строковое сравнение (`chapter_10` < `chapter_9`). `natural_key` разбивает имя на числовые и текстовые куски и сравнивает числа как числа (`chapter_9` < `chapter_10`). При отсутствии нулевого паддинга в именах файлов эти две сортировки дают **разный порядок**, а значит `--start 60 --end 60` в PS и в Python указывают на **разные главы**.

Последствия конкретны: `$SelectedChapterStems` используются в `Remove-QualityArtifacts` и `Get-RetryCount`, `$SelectedInputFiles` — в `Remove-SelectedOutputs`. То есть runner может чистить артефакты и считать retry для главы X, пока Python обрабатывает главу Y. `Get-RetryCount` в этом случае не найдёт `status.json` (`if (-not (Test-Path $path)) { continue }`) и вернёт **0** — цикл repair завершится досрочно как «всё разрешено», хотя ни один retry не проверялся.

**Trigger scenario.** Зависит от фактических имён файлов в `pact_chapters`, которых в пакете нет. Если имена вида `060_...html` — расхождения нет. Если `pact_60.html` рядом с `pact_100.html` — есть.

**Impact.** Потенциально: ложное `retry_required=0`, чистка не тех артефактов, работа не над той главой. Особенно опасно при переходе к многоглавным прогонам, ради которых пайплайн и строится.

**Минимальное исправление.** Привести PS к натуральной сортировке:
```powershell
$AllInputFiles = @(Get-ChildItem (Join-Path $ProjectRoot 'pact_chapters') -Filter '*.html' -File |
    Sort-Object @{Expression={ [regex]::Replace($_.BaseName, '\d+', { param($m) $m.Value.PadLeft(10,'0') }) }})
```
и дополнительно — жёсткая сверка: заставить `prepare_pipeline_context.py` печатать выбранные stem-ы, а runner сравнивать их с `$SelectedChapterStems` и падать при расхождении. Второе важнее первого: оно защищает от любых будущих расхождений, а не только от этого.

**Regression test.** Оффлайн: каталог с `c_2.html`, `c_10.html`, `c_60.html`; вызвать `select_files(cfg, 2, 2)` и PS-выборку, сравнить имена.

**Побочные эффекты.** Если файлы уже именуются с паддингом, изменение сортировки ничего не меняет — безопасно.

---

### CLD-008 — `changed_pids` завышен из-за нормализации пробелов

- **severity:** MEDIUM · **status:** CONFIRMED · **active path:** YES
- **category:** quality (reporting)
- **файлы:** `v31_common.py` → `load_translations` (121-126), `norm` (47-48); `v31_adjudicate.py:181`; `v31_finalize_quality.py:214, 242-249`; `v31_build_review.py`

**Механизм.** `load_translations` прогоняет каждое значение через `norm`, то есть `re.sub(r"\s+", " ", …).strip()`. `v31_adjudicate` записывает **всю** нормализованную карту в `v31_{primary|final}_translations.json`, включая PID, которых никто не касался. Финальный gate сравнивает:
```python
draft = read_json(work / "draft_translations.json", {})     # НЕ нормализован
...
changed_pids = [pid for pid in expected_pids if draft.get(pid) != translations.get(pid)]
```
Любой PID, где черновик содержал двойной пробел, неразрывный пробел или перевод строки, попадает в `changed_pids`, хотя текст не правился.

Дальше это число подставляется как `"changed_candidates"` и `"accepted"` в `post_repair_report.json` (строки 242-243) и как «Changed PIDs» в review-отчёте и мониторе.

**Impact.** Метрика, по которой предполагается судить об объёме вмешательства («хорошая часть перевода не должна переписываться без необходимости»), недостоверна. При разборе прогона это приведёт к поиску несуществующих правок. Сам текст не портится — нормализация пробелов внутри абзаца для прозы безвредна.

**Минимальное исправление.** Сравнивать в одной нормализации:
```python
changed_pids = [pid for pid in expected_pids if norm(draft.get(pid)) != norm(translations.get(pid))]
```
`norm` уже импортирован в `v31_finalize_quality.py`. Аналогично в `v31_build_review.py` (там `changed = [pid for pid in final if draft.get(pid) != final.get(pid)]`).

**Regression test.** Fixture: draft с `"Он  ушёл."` (два пробела), final с `"Он ушёл."` → `changed_pids` должен быть пуст.

**Побочные эффекты.** Нет. Чисто отчётное исправление.

---

### CLD-009 — жёсткий детерминированный инвариант может быть отклонён одним судьёй

- **severity:** MEDIUM · **status:** CONFIRMED · **active path:** YES
- **category:** merge
- **файлы:** `v31_merge_issues.py:123-140`; `v31_finalize_verification.py:140-141`

**Механизм.** Быстрый путь для жёстких инвариантов требует, чтобы issue был найден **только** детерминированной проверкой:
```python
elif families == {"deterministic"} and category in HARD_DETERMINISTIC:
    issue.update({"verification_route": "hard_deterministic", "verification_decision": "repair", ...})
```
Но если ту же проблему заодно заметила модель, `families` становится `{"deterministic", "gemma"}` и условие не выполняется. Issue уходит по ветке `elif "deterministic" in families` к **одному** судье, и его вердикт `keep/high` переводит issue в `rejected` — жёсткий инвариант отменён мнением одной модели.

Страховка есть: `v31_finalize_quality` пересчитывает детерминированные проверки на финальном тексте и блокирует по `fail_deterministic_categories`. Но результат — не исправление, а **аварийная остановка в самом конце прогона**, после всех затрат.

**Trigger scenario.** Пропущенное предложение (`missing`), которое Gemma тоже отметила как `missing`. Судья Qwen решает, что перевод допустим, ставит `keep/high`. Issue отклонён, repair не запускается, финальный gate падает с `final_deterministic`.

**Impact.** Потеря дорогого прогона вместо дешёвой автоматической правки; логика «жёсткий инвариант неоспорим» нарушена в самом частом случае — когда ошибка настолько очевидна, что её видят и детектор, и модель.

**Минимальное исправление.** Проверять категорию раньше семейств:
```python
if "deterministic" in families and category in HARD_DETERMINISTIC:
    issue.update({"verification_route": "hard_deterministic",
                  "verification_decision": "repair",
                  "verification_confidence": "deterministic",
                  "verification_reason": "Hard deterministic invariant failed."})
    preverified.append(issue)
elif "qwen" in families and "gemma" in families:
    ...
```
То есть поднять ветку `hard_deterministic` выше ветки согласия семейств и заменить `families == {"deterministic"}` на `"deterministic" in families`.

**Regression test.** Оффлайн: issue категории `missing` с `detected_by=["deterministic","gemma_semantic_primary"]` → ожидать `verification_route == "hard_deterministic"` и попадание в `preverified`.

**Побочные эффекты.** Чуть больше issue уходит в repair без судьи. Для `missing` (и `mixed_script` после CLD-002) это оправданно: обе категории объективны и проверяются детерминированно ещё раз в `v31_deterministic_gate`.

---

### CLD-010 — монитор объявляет завершение при наличии старого output

- **severity:** MEDIUM · **status:** CONFIRMED · **active path:** YES (операционный инструмент)
- **category:** monitor
- **файлы:** `monitor_pipeline_v31.ps1:257-262`

**Механизм.**
```powershell
$outputs = if (Test-Path $OutputDir) { @(Get-ChildItem $OutputDir -File) } else { @() }
if ($outputs.Count -gt 0) { Write-Host 'PIPELINE V3.1 COMPLETE' -ForegroundColor Green }
```
Условие — просто непустая директория. При запуске **без** `-Reset` (а `-Reset` удаляет весь run root) старый HTML предыдущего прогона лежит на месте, и монитор с первой же секунды показывает зелёное `PIPELINE V3.1 COMPLETE`, пока pipeline только начинает перевод.

Дополнительно `$ErrorActionPreference = 'SilentlyContinue'` на весь скрипт (строка 10) прячет ошибки чтения, так что расхождения выглядят как «пусто», а не как сбой.

**Impact.** Ложный сигнал завершения при resume-прогонах; риск преждевременно собрать bundle и разбирать чужой результат.

**Минимальное исправление.** Привязать сигнал к состоянию, а не к наличию файла:
```powershell
$state = Read-JsonSafe (Join-Path $work 'state.json')
$done = ($state -and $state.status -eq 'complete' -and $quality -and $quality.ok)
```
и печатать `COMPLETE` только при `$done` для всех глав диапазона.

**Regression test.** Ручной: положить произвольный файл в `output/`, запустить монитор с `-Once` — не должно быть `COMPLETE`.

**Побочные эффекты.** Нет.

---

### CLD-011 — ledger разрастается при каждом повторном запуске

- **severity:** MEDIUM · **status:** CONFIRMED · **active path:** YES
- **category:** other (data integrity)
- **файлы:** `v31_source_analysis.py:340-349`, `merge_address_matrix` (273-319), `current_address_view` (264-270)

**Механизм.** На пути переиспользования кеша:
```python
if out.exists() and not args.force:
    existing = read_json(out, {})
    book_ledger["address_matrix"] = merge_address_matrix(
        book_ledger.get("address_matrix") or {},
        [dict(value) for value in (existing.get("address_matrix") or {}).values() ...],
        source_path.name)
```
Но `existing["address_matrix"]` — это `current_address_view(effective_matrix)`, то есть **уже слитая матрица всей книги**, а не обновления данной главы. Она сливается сама с собой. `current_address_view` вырезает `history`, поэтому в `merge_address_matrix` каждая запись приходит как «новое свидетельство» и в ветке совпадающего регистра выполняется `history.append(dict(incoming))`.

Итог: при каждом повторном запуске (без `--force`) `history` каждого ключа удлиняется на одну запись, и в ней теряется исходная привязка к главе — `incoming["chapter"]` перезаписывается текущей главой.

Второстепенно: записи с `expected_register: "unknown"` (результат конфликта) при обратном слиянии попадают в ветку `if inc_register == "unknown": continue` — значение не портится, но история всё равно растёт. И `update['speaker']` (строка 278) бросит KeyError, если в матрице окажется запись без `speaker`.

**Impact.** Неограниченный рост `book_consistency_ledger.json` на длинной книге; `history` перестаёт быть достоверной летописью регистров; `chapter` в записях указывает на последний прогон, а не на источник свидетельства. Поскольку матрица подаётся в промпт source analysis (`current_address_view`), в перспективе это ещё и раздувание контекста.

**Минимальное исправление.** На пути переиспользования не сливать ничего:
```python
if out.exists() and not args.force:
    logging.info("Reusing %s", out)
    continue
```
Матрица уже была слита в ledger при первом (успешном) прогоне этой главы — повторное слияние избыточно по определению. Если нужна страховка на случай потери ledger, сохранять рядом с `source_scene_map.json` отдельный файл `address_updates.json` с **обновлениями главы** и сливать именно его.

**Regression test.** Оффлайн: запустить `v31_source_analysis.py` дважды подряд без `--force` на fixture-главе; длина `history` каждого ключа в ledger должна совпадать.

**Побочные эффекты.** Нет, при условии что ledger не удаляется отдельно от `source_scene_map.json`. Сейчас runner удаляет ledger при `-RedoSourceAnalysis`/`-RedoTranslation` — а `source_scene_map.json` в этих случаях тоже пересчитывается (`--force`), так что инвариант сохраняется.

---

### CLD-012 — legacy post-repair gate физически не может сработать

- **severity:** MEDIUM · **status:** CONFIRMED · **active path:** YES
- **category:** quality
- **файлы:** `v31_finalize_quality.py:234-250`; `pact_translate_v3.py` → `Runner.finalize` (3217-3227)

**Механизм.** `v31_finalize_quality` пишет отчёт с константами:
```python
"retry_required": 0,
"unresolved_total": 0,
"unresolved_issue_count": 0,
```
`Runner.finalize` читает ровно эти поля:
```python
unresolved = int(post_report.get("unresolved_total", post_report.get("retry_required", 0)))
if unresolved: raise PipelineError(...)
```
Значение всегда 0 → проверка всегда проходит. При этом конфиг демонстративно включает её: `post_repair['enabled']=$true`, `['required']=$true`, `['fail_on_unresolved']=$true`.

Формально это безопасно: `v31_finalize_quality` уже упал бы раньше, если бы что-то осталось нерешённым (там проверки настоящие и довольно плотные). Но два следствия неприятны:
1. создаётся **видимость** второго независимого барьера, которого нет;
2. если `v31_finalize_quality.py` когда-либо будет пропущен, отключён или отработает по устаревшим данным, финализация не будет иметь никакой защиты вообще — `--phase finalize` возьмёт `repaired_translations.json` каким есть.

**Impact.** Иллюзия защиты. Это тот же класс, что и CLD-006: статус зелёный по построению, а не по проверке.

**Минимальное исправление.** Заполнять поля из фактических данных, они уже посчитаны в этой же функции:
```python
unresolved_lifecycle_total = sum(
    1 for row in lifecycle
    if row.get("status") not in {"resolved_repair", "resolved_false_positive"})
...
"retry_required": unresolved_lifecycle_total,
"unresolved_total": unresolved_lifecycle_total,
"unresolved_issue_count": unresolved_lifecycle_total,
```
Значение по-прежнему будет 0 при нормальном прогоне (иначе gate упал бы выше), но станет **следствием проверки**, а не константой.

Дополнительно стоит записать в `post_repair_report.json` поле `"gate_version": VERSION` и `"generated_at"`, а в `Runner.finalize` — проверять, что отчёт новее `repaired_translations.json`. Это закроет сценарий устаревшего отчёта.

**Regression test.** Fixture с одной lifecycle-записью в статусе `retry_required` → `post_repair_report.unresolved_total == 1` и падение `Runner.finalize`.

**Побочные эффекты.** Нет.

---

### CLD-013 — `english_sequence_min_words=2` создаёт неустранимый конфликт правил

- **severity:** MEDIUM · **status:** CONFIRMED (расхождение) / LIKELY (тупик repair) · **active path:** YES
- **category:** repair
- **файлы:** `run_full_pipeline_v31.ps1:132`; `pact_translate_v3.py` → `detect_english_sentence` (1161-1171), `validate_single_repair` (2352-2356), `deterministic_issues` (1763-1774)

**Механизм.** Runner понижает порог с 5 (`DEFAULTS`) до 2. Этот параметр используется в трёх разных ролях:
1. `validate_single_repair` — **отклоняет кандидата** с английским остатком;
2. `deterministic_issues` — порождает `english_residue` severity `critical`;
3. `final_integrity` — только warning.

При пороге 2 достаточно двух латинских слов в предложении при условии `latin >= 2*cyrillic`. В коротких PID (реплика диалога, восклицание) это условие выполняется легко.

Возникает потенциальный тупик: если в исходнике есть фрагмент, который **должен** остаться латиницей (название, формула, вывеска), `deterministic_issues` пометит его как `english_residue` (блокирующая категория в финальном gate), а `validate_single_repair` отклонит **любого** кандидата, который его сохраняет. Repair не сможет удовлетворить оба требования, `v31_deterministic_gate` будет фиксировать `Did not resolve deterministic category: english_residue`, и после `max_repair_rounds=3` прогон упадёт с `"primary left N unresolved PID(s)"`.

Почему LIKELY, а не CONFIRMED: срабатывание зависит от наличия таких фрагментов в конкретной главе, чего я по коду установить не могу.

**Impact.** При наличии намеренной латиницы — неустранимый цикл и падение прогона. Без неё — повышенная строгость, которая, возможно, желаема.

**Минимальное исправление.** Развязать роли: ввести отдельный параметр для детектора issue и для валидации кандидата.
```powershell
$validation['english_sequence_min_words'] = 2          # для issue-детектора
$validation['repair_english_min_words'] = 4            # для validate_single_repair
```
и в `validate_single_repair` читать `cfg["validation"].get("repair_english_min_words", cfg["validation"]["english_sequence_min_words"])`. Плюс завести whitelist разрешённой латиницы (общий с CLD-002).

**Regression test.** Оффлайн: `validate_single_repair` на кандидате, содержащем короткую допустимую латинскую вставку, при `repair_english_min_words=4` → ошибок нет; при 2 → есть.

**Побочные эффекты.** Ослабление проверки кандидатов. Компенсируется тем, что `english_residue` как issue остаётся на пороге 2 и по-прежнему блокирует финализацию.

---

### CLD-014 — отсутствие валидации схемы при чтении кешей

- **severity:** LOW · **status:** CONFIRMED · **active path:** YES
- **category:** other (robustness)
- **файлы:** `v31_adjudicate.py:158`; `v31_cross_verify.py:141-142`; `v31_repair.py:202-203`; `v31_postcheck.py:232-233`; `v31_audit.py:393-395`

**Механизм.** Все стадии читают кеш через `read_json(cache, {})` и сразу используют результат без проверки формы. Конкретные точки отказа:
- `v31_adjudicate.py:158`: `record.get("candidates", [{}])[0]` — при `candidates == []` даёт **IndexError**, а не значение по умолчанию (default сработает только при полном отсутствии ключа);
- `v31_cross_verify.py:159-160`: `decisions.append(record)` после чтения кеша; при усечённом файле `record` = `{}` → `v31_finalize_verification.py:59` падает на `record["issue_id"]` KeyError;
- `v31_repair.py:225`: `records.append(record)` → `record["pid"]` в `v31_postcheck.py:228` KeyError.

Файлы пишутся атомарно (`v31_common.write_json` через `.tmp` + `replace`), поэтому усечение маловероятно. Но кеш может остаться от **другой версии схемы** после патча — а версия в записях есть (`"version": VERSION`) и не проверяется нигде.

**Impact.** Аварийное падение стадии вместо пересчёта; при смене схемы — необъяснимые KeyError вместо понятного сообщения.

**Минимальное исправление.** Одна общая функция в `v31_common.py`:
```python
def read_cache(path: Path, required: Sequence[str]) -> dict | None:
    if not path.exists():
        return None
    try:
        data = read_json(path, {})
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None
    return data if all(key in data for key in required) else None
```
и в местах чтения кеша: `record = read_cache(cache, ("pid", "candidates"))`; если `None` — пересчитывать, а не падать. Отдельно исправить `v31_adjudicate.py:158` на `(record.get("candidates") or [{}])[0]`.

**Regression test.** Положить `{}` в файл кеша кандидата, запустить стадию — ожидать пересчёт, а не исключение.

**Побочные эффекты.** После патчей схемы кеши будут инвалидироваться автоматически (это плюс, но первый прогон после апдейта станет дороже).

---

### CLD-015 — `Stop-LlamaServer` убивает посторонние процессы

- **severity:** LOW · **status:** CONFIRMED · **active path:** YES
- **category:** other
- **файл:** `run_full_pipeline_v31.ps1:168`

**Механизм.** После корректной остановки собственного дочернего процесса выполняется `Get-Process llama-server | Stop-Process -Force` — без фильтра по PID. Любой `llama-server`, запущенный пользователем для других задач (в том числе постоянный сервер для Open WebUI), будет убит при каждом переключении профиля, а их в прогоне десятки.

**Impact.** Побочные разрушения вне pipeline. Для этой машины актуально: на десктопе используется llama.cpp и для других целей.

**Минимальное исправление.** Ограничить зачистку процессами, слушающими нужный порт, либо запомнить PID:
```powershell
Get-Process llama-server -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $LlamaExe -and $_.StartTime -ge $script:RunStartedAt } |
    Stop-Process -Force -ErrorAction SilentlyContinue
```
с `$script:RunStartedAt = Get-Date` в начале скрипта.

**Regression test.** Ручной: запустить посторонний `llama-server` на другом порту, прогнать одну смену профиля, проверить, что он жив.

**Побочные эффекты.** Если «висячий» процесс от предыдущего упавшего прогона занимает порт 8080, он больше не будет убит автоматически, и `Start-LlamaServer` упадёт по таймауту готовности. Сообщение об ошибке стоит дополнить подсказкой про освобождение порта.

---

### CLD-016 — неатомарная запись `chapter_bible.json`

- **severity:** LOW · **status:** CONFIRMED · **active path:** YES
- **category:** other
- **файл:** `prepare_pipeline_context.py:24-26`

**Механизм.** Локальный `write_json` пишет напрямую:
```python
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
```
без `.tmp` + `replace`, в отличие от `v31_common.write_json` и `pact_translate_v3.atomic_json`. Прерывание (Ctrl+C, падение питания) на записи `chapter_bible.json` оставит усечённый JSON. При следующем запуске сработает ветка `if bible_path.exists()` → `read_json` бросит `JSONDecodeError`, и стадия 1 будет падать до ручного удаления файла.

Отдельно: `glossary_enforcement.changes` при повторном прогоне пересчитывается только по изменениям текущего прохода, поэтому после resume список выглядит пустым, хотя переопределения были применены ранее. Это ухудшает диагностику, но не данные.

**Минимальное исправление.** Скопировать атомарную реализацию:
```python
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
```

**Regression test.** Не нужен отдельный; покрывается ревью.

**Побочные эффекты.** Нет.

---

## 4. Likely defects / risks

### CLD-017 — `token_count_url` может не существовать, а откат молчалив

- **severity:** MEDIUM · **status:** LIKELY · **active path:** YES · **category:** other
- **файлы:** `pact_translate_v3.py` → `ApiClient.token_count` (402-420), `_post` (358-382), `fit_output_budget` (834-849)

Конфиг задаёт `token_count_url = http://127.0.0.1:8080/v1/chat/completions/input_tokens`. Такого эндпоинта в стандартном `llama-server` я не знаю (там `/tokenize`). Если он отсутствует, происходит следующее: `_post` делает `http_retries=3` попытки с `retry_delay_seconds=8` между ними, бросает `PipelineError`, а `token_count` его молча ловит и возвращает эвристику `sum(len(content))/2.6`.

Последствия, если гипотеза верна:
1. **≈16 секунд сна + 3 неудачных HTTP-запроса на каждый вызов `fit_output_budget`**, то есть на каждый вызов модели во всём pipeline. При тысячах вызовов это часы впустую;
2. бюджет вывода считается по символьной эвристике, а не по токенам — для русского текста с кириллицей отношение символов к токенам заметно отличается от 2.6, так что защита `limit < 256 → "Prompt too large"` работает по неверной оценке;
3. в логе это видно только как `WARNING … token endpoint unavailable`, что легко потерять среди прочего.

Почему LIKELY: наличие эндпоинта в сборке 9721 (5fd2dc2c4) я по пакету проверить не могу — нужен лог прогона.

**Как проверить:** `Select-String -Path logs\run_*.log -Pattern 'token endpoint unavailable' | Measure-Object`. Ноль совпадений — гипотеза неверна, находку закрыть.

**Минимальное исправление (если подтвердится).** Кешировать факт недоступности, чтобы не платить ретраями каждый раз:
```python
def token_count(self, messages, stage) -> int:
    if getattr(self, "_token_endpoint_dead", False):
        return math.ceil(sum(len(m["content"]) for m in messages) / 2.6)
    try:
        ...
    except Exception as exc:
        self._token_endpoint_dead = True
        logging.warning("%s token endpoint disabled after failure: %s", self.name, exc)
        return estimate
```
и параллельно — перевести `token_count_url` на реально существующий `/tokenize`, разобрав длину массива токенов.

---

### CLD-018 — regex preflight может не совпасть с форматом лога

- **severity:** MEDIUM · **status:** QUESTION → LIKELY · **active path:** YES · **category:** other
- **файл:** `run_full_pipeline_v31.ps1:263-274`

Скорость генерации вытаскивается так:
```powershell
'(?m)^.*?\|\s+eval time\s*=\s*[\d.]+\s*ms\s*/\s*\d+\s*tokens.*?([\d.]+)\s*tokens per second'
```
Шаблон требует символа `|` перед `eval time` — это привязка к конкретному формату строк llama.cpp, который менялся между сборками. Если формат отличается, `$generationMatches.Count -eq 0` → `throw "Gemma preflight could not parse timing data"` → прогон не стартует вообще.

Обратный риск тоньше: шаблон prompt (строка 265) не содержит `|` и не исключает `prompt eval time` — но так как он ищет именно `prompt eval time`, коллизии нет. А вот generation-шаблон исключает prompt-строки только через требование `|`, что хрупко. В мониторе (`monitor_pipeline_v31.ps1:85-88`) для той же задачи используется более надёжный подход: `-match '\beval time\s*='` **и** `-notmatch 'prompt eval time'`. Логику монитора стоит перенести в preflight.

**Как проверить:** взять любой `server_logs\GemmaTranslate_*_stderr.log` из текущего прогона и прогнать оба regex.

**Минимальное исправление.** Заменить generation-шаблон на подход монитора: сначала отфильтровать строки, содержащие `eval time` и не содержащие `prompt eval time`, затем извлечь число перед `tokens per second`.

---

### CLD-019 — `alternative_for_multiple_issues` не подключён

- **severity:** LOW · **status:** CONFIRMED · **active path:** YES · **category:** other
- **файл:** `v31_repair.py:44-50`

Флаг присутствует в `DEFAULT_STAGE` и в generated config, но в вычислении `difficult` не участвует:
```python
difficult = (len(issues) > 1 or any(category in difficult_categories ...) or any(scope in {...}) or bool(feedback))
```
Выключить генерацию альтернативы через конфиг нельзя. Исправление тривиально:
```python
allow_alt = bool(stage.get("alternative_for_multiple_issues", True))
difficult = allow_alt and (len(issues) > 1 or ...)
```
Отношу к рискам, а не к дефектам: текущее поведение (всегда два кандидата на сложных случаях) само по себе разумно, проблема лишь в том, что параметр вводит в заблуждение при разборе конфига.

---

### CLD-020 — размер `attempts` при split не отражает реальную стоимость

- **severity:** LOW · **status:** LIKELY · **active path:** YES · **category:** other
- **файлы:** `v31_audit.py:437-445`; `v31_source_analysis.py:402-413`

В `v31_audit.py` при split-восстановлении запись `attempts` заменяется одним синтетическим элементом `[{"ok": False, "error": ..., "split_recovery": True}]` — фактические попытки родительского батча теряются. В `v31_source_analysis.py` эта же проблема решена правильно (используется `getattr(exc, "attempt_errors", [])` из `JsonGenerationError`), а в аудите — нет. Из-за этого статистика по неудачным попыткам аудита занижена, и `truncated_json`-подобный анализ для аудитов недоступен.

Исправление симметрично source analysis: перехватывать `JsonGenerationError` и брать `exc.attempt_errors`.

---

## 5. Questions requiring run evidence

**QST-001 — фактические имена файлов глав.**
Нужен листинг `pact_chapters`. Определяет, реализуется ли CLD-007 на практике. Проверка: `Get-ChildItem pact_chapters -Filter *.html | Select-Object -First 5 Name` и сравнение с `python pact_translate_v3.py --config … --plan`.

**QST-002 — доступность `token_count_url`.**
Определяет статус CLD-017. Проверка: грепнуть `token endpoint unavailable` в `logs\run_*.log`.

**QST-003 — фактический формат timing-строк llama.cpp сборки 9721.**
Определяет статус CLD-018. Проверка: прогнать оба regex по свежему `*_stderr.log`.

**QST-004 — реальная частота `uncertain`.**
Сколько issue за главу 60 попало в `uncertain_issues.json` до падения (если падение было). Определяет, насколько срочен CLD-003 и какая `uncertain_policy` уместна. Файл: `work/*/v31/*/uncertain_issues.json`.

**QST-005 — реальная доля `independent_detector_agreement`.**
Сколько issue обошло cross-verification. Считается по `merged_issues.json`: `[i for i in issues if i["verification_route"] == "independent_detector_agreement"]`. Даёт масштаб CLD-001.

**QST-006 — фактическое число formatting incidents.**
Файл `work/*/formatting/batch_*.json`, поле `incidents`, плюс `quality_report.json → integrity.warnings`. Даёт масштаб CLD-006 и позволяет выбрать разумный `max_incidents`.

**QST-007 — присутствует ли в главе намеренная латиница.**
Определяет статус тупика в CLD-013 и необходимость whitelist для CLD-002.

**QST-008 — поддерживаются ли флаги `-fit` / `-fitt` сборкой 9721.**
В профилях Qwen/GemmaVerify/GemmaRepair используются `-fit on -fitt N`. Если сборка их не знает, `llama-server` завершится, и `Start-LlamaServer` бросит `"… exited. See …"`. Судя по тому, что прогоны идут, флаги поддерживаются, но подтвердить по пакету не могу. Проверка: `llama-server --help | Select-String 'fitt'`.

---

## 6. Test gaps

Текущий `self_test_v31.py` покрывает пять контрактов: слияние двух issue с одинаковым спаном, `parse_candidates` для `replace_span`, `parse` semantic gate, совместимость `compatible_issue` с dataclass `Issue`, наличие маркера source-analysis в промпте перевода. Этого мало, и характерно, что **ни один из найденных дефектов существующими тестами не ловится**. Более того, единственный merge-тест (`merged[0]["agreement_count"] == 2`) закрепляет как правильное именно то поведение, которое порождает CLD-001.

Непокрытые области, ранжированные по риску:

| Область | Что не проверяется | Связанные находки |
|---|---|---|
| Merge/маршрутизация | слияние разных категорий; выбор `verification_route`; условия преверификации | CLD-001, CLD-009 |
| Детерминированные категории | соответствие между списком порождаемых категорий и тремя списками блокирующих | CLD-002 |
| Консолидация вердиктов | таблица (decision, confidence) × 2 судьи → verified/rejected/uncertain | CLD-003 |
| Инвалидация resume | какие флаги какие артефакты удаляют | CLD-004 |
| Учёт покрытия | соответствие `coverage` в quality gate фактически проверенному тексту | CLD-005 |
| Финальная целостность | реакция на formatting incidents | CLD-006 |
| Выбор глав | эквивалентность PS- и Python-сортировки | CLD-007 |
| Нормализация | согласованность сравнений draft/final | CLD-008 |
| Устойчивость кешей | повреждённые и устаревшие по схеме записи | CLD-014 |
| Ledger | идемпотентность повторного слияния | CLD-011 |

Дополнительно: `self_test_v31.py` вставляет в `sys.path` собственную директорию и импортирует `v31_*`. В пакете он лежит в `03_SUPPORTING_TOOLS`, а модули — в `02_CURRENT_CODE/runner`; в продакшене они, судя по всему, в одной папке. Это неявная зависимость от раскладки, которую стоит зафиксировать явным `--runner-dir`.

Конкретные тесты — в `CLAUDE_TEST_PLAN_RU.md`.

---

## 7. Приоритетный минимальный patch plan

Порядок выбран так, чтобы каждый шаг был независимо проверяем и не требовал модели.

**Этап 1 — до любого следующего полного прогона (все правки оффлайн-тестируемы)**

| Шаг | Находка | Файл | Объём |
|---|---|---|---|
| 1.1 | CLD-002 | `pact_translate_v3.py` → `deterministic_issues` + `DEFAULTS["deterministic_qa"]` | ~12 строк |
| 1.2 | CLD-001 | `v31_common.py` → `merge_duplicate_issues` (+ `merge_reason`); `v31_merge_issues.py` → условие преверификации | ~10 строк |
| 1.3 | CLD-009 | `v31_merge_issues.py` → поднять ветку `hard_deterministic` | ~4 строки |
| 1.4 | CLD-003 | `v31_finalize_verification.py` → согласие при разной уверенности + `uncertain_policy` | ~15 строк |

После этапа 1 — прогон главы 60 с `-Reset` и сравнение числа issue по маршрутам с QST-005.

**Этап 2 — до запуска книги**

| Шаг | Находка | Файл |
|---|---|---|
| 2.1 | CLD-006 | `final_integrity` + `run_formatting` + `DEFAULTS["formatting"]["max_incidents"]` |
| 2.2 | CLD-005 | `v31_finalize_quality.py` → маркер неаудированных PID (вариант 1), затем повторный discourse (вариант 2) |
| 2.3 | CLD-004 | `run_full_pipeline_v31.ps1` → инвалидация при `-RedoSourceAnalysis` |
| 2.4 | CLD-007 | `run_full_pipeline_v31.ps1` → натуральная сортировка + сверка stem-ов с Python |
| 2.5 | CLD-013 | развязка порогов + whitelist латиницы (общий с 1.1) |

**Этап 3 — гигиена, можно параллельно**

CLD-008 (сравнение через `norm`), CLD-010 (монитор), CLD-011 (ledger), CLD-012 (реальные значения в `post_repair_report`), CLD-014 (`read_cache`), CLD-015 (точечная остановка сервера), CLD-016 (атомарная запись), CLD-019, CLD-020.

**Этап 4 — только после ответов на QST**

CLD-017 и CLD-018 трогать до получения логов не нужно: обе правки безопасны, но без данных непонятно, решают ли они реальную проблему.

---

## 8. Что не следует менять

Перечисляю явно, чтобы при исправлениях не задеть работающее.

1. **Профили моделей.** Ни один флаг Qwen/Gemma не является причиной найденных дефектов. Все находки — логика на уровне Python/PowerShell. Деградация производительности, описанная в разделе 5 handoff, диагностирована верно как состояние Windows/Vulkan.

2. **Разделение GemmaRepair и GemmaTranslate.** Отдельный профиль без MTP для repair оправдан и не должен объединяться обратно.

3. **`strict_bool`.** Строгая проверка булевых полей в `v31_postcheck.parse` — правильное решение, защищающее от `"true"`-строк, которыми модели любят отвечать. Не ослаблять.

4. **Требование `confidence == "high"` в `semantic_accept` / `russian_accept`.** Это ключевой барьер против самоподтверждения. Ослабление здесь напрямую вернёт проблемы, описанные в разделе 7 handoff.

5. **Проверка `"unchanged"` в `validate_single_repair`.** Закрывает регрессию PID `p00034`. Не трогать.

6. **Запрет `challenge_issue` для жёстких категорий** (`v31_deterministic_gate.py:71-73`). Работает корректно.

7. **Изоляция контекста аудитов.** То, что `gemma_semantic` намеренно не получает Qwen SOURCE NOTES, а `gemma_russian` не видит английский — сознательное и правильное решение. При правках `build_prompt`/`*_messages` эту асимметрию нужно сохранить.

8. **Рекурсивное деление батчей.** Логика split в `v31_audit.py` и `v31_source_analysis.py` корректна, покрытие после split пересобирается верно, бесконечной рекурсии нет (граница `len(pids) <= 1`).

9. **Атомарная запись в `v31_common.write_json`.** Уже правильная; менять надо только `prepare_pipeline_context.py`, чтобы соответствовал.

10. **Accounting-инварианты `v31_finalize_quality`** (сверки merged/verification/decision totals, queue vs report, lifecycle vs verified ids). Они плотные и написаны аккуратно — это лучшая часть кодовой базы. Добавлять к ним стоит, убирать — нет.

11. **Выбор кандидата по минимальному `changed_ratio`.** Соответствует требованию «не переписывать хорошее без необходимости».

12. **Общий подход «не доверять одной модели».** Дефект CLD-001 — это ошибка реализации конкретного условия, а не порок архитектуры. Чинить надо условие, а не схему.
