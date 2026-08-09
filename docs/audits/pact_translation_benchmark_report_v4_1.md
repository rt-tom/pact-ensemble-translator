# Pact Translation Benchmark — сводный отчёт v4.1

## Цель

Отчёт фиксирует два связанных эксперимента по **Pact — Bonds 1.1**:

1. слепое сравнение пяти вариантов на одном и том же фрагменте примерно из 110 PID;
2. отдельное сравнение **Independent №1** и **Pipeline Remote Reasoning v4.1** на значительно большем объёме первой главы.

Важно не смешивать эти две части: первая лучше подходит для чистого сравнения моделей/режимов, вторая — для оценки влияния translation-stage pipeline при одинаковой основной модели и High reasoning.

---

# Часть I. T1 vs T2 vs T3 vs Independent №1 vs Independent №2

## Конфигурации

### T1
- локальная модель **T-lite**
- blind run
- итоговая оценка: **≈5.0/10**

### T2
- локальная **Gemma**
- reasoning: **0**
- blind run
- итоговая оценка: **≈8.0–8.1/10**

### T3
- локальная **Qwen**
- reasoning: **0**
- blind run
- итоговая оценка: **≈6.7/10**

### Independent №1
- **DeepSeek Flash**
- reasoning: **High**
- whole-chapter direct translation
- без pipeline
- итоговая оценка на тех же ~110 PID: **≈8.3/10**

Использованный prompt:

```text
Translate the following book chapter into natural, polished literary Russian, as a professional fiction translator would. Preserve the meaning, tone, character voices, humor, profanity, and paragraph structure. Avoid literal or awkward English calques. Do not omit, summarize, or add anything. Output only the Russian translation.
```

### Independent №2
- та же **DeepSeek Flash**
- reasoning: **0**
- тот же исходник
- тот же prompt
- whole-chapter direct translation
- без pipeline
- итоговая оценка: **≈7.4/10**

---

## Итоговый рейтинг

| Место | Вариант | Конфигурация | Оценка |
|---:|---|---|---:|
| 1 | **Independent №1** | DeepSeek Flash, High reasoning | **≈8.3** |
| 2 | **T2** | Local Gemma, reasoning 0 | **≈8.0–8.1** |
| 3 | **Independent №2** | DeepSeek Flash, reasoning 0 | **≈7.4** |
| 4 | **T3** | Local Qwen, reasoning 0 | **≈6.7** |
| 5 | **T1** | Local T-lite | **≈5.0** |

---

## Independent №1 — сильнейший литературный baseline

Главные преимущества:

- лучший character voice;
- лучший emotional register;
- сильнее anti-calque;
- более естественный русский;
- лучше диалоги;
- лучше ритм;
- точнее передаёт силу мата.

Характерные решения:

```text
Jesus fuck → Господи блядь
fuck off → отъебись
```

Слабость: High reasoning не устраняет semantic errors полностью. Например, `No parking` был передан как «Остановка запрещена», а `wannabe-architect` получил пол, которого оригинал не задаёт.

Вывод:

> **High reasoning существенно улучшает литературную обработку, но не заменяет semantic QA.**

---

## T2 — Local Gemma reasoning 0

Главный положительный сюрприз benchmark.

Gemma показала:

- очень хороший естественный русский;
- высокую semantic fidelity;
- хорошую связность;
- сильные диалоги;
- хорошую перестройку английского синтаксиса;
- мало грубых ошибок;
- заметно меньше кальки, чем DeepSeek reasoning=0.

На одинаковом ~110-PID фрагменте T2 уступил Independent №1 всего примерно на **0.2–0.3 балла**.

### Где T2 была даже точнее

```text
No parking
Independent №1 → Остановка запрещена
T2             → Парковка запрещена
```

```text
weasel
Independent №1 → хорёк
T2             → ласка
```

### Главная слабость

Gemma чаще смягчает эмоциональный регистр:

```text
Jesus fuck
Independent №1 → Господи блядь
T2             → Господи помилуй
```

```text
fuck off
Independent №1 → отъебись
T2             → отвали
```

Вывод:

> **Gemma reasoning=0 уже является полноценным сильным literary translator, а не просто дешёвым компромиссом.**

---

## Independent №2 — DeepSeek reasoning 0

Это почти чистый A/B-тест против Independent №1:

```text
same model
same chapter
same prompt
same whole-chapter setup
different reasoning
```

Результат:

```text
DeepSeek High ≈8.3
DeepSeek 0    ≈7.4
```

При reasoning=0 стало больше:

- кальки;
- source-shaped Russian;
- нейтрализации character voice;
- сглаживания мата;
- первого «достаточно хорошего» соответствия вместо литературной reformulation.

Рабочая гипотеза:

> **Reasoning у DeepSeek заметно участвует в chapter-level planning, voice preservation и anti-calque reformulation.**

---

## T3 — Local Qwen reasoning 0

Сильные стороны:

- обычно правильно понимает общий смысл;
- иногда хорошо сохраняет source detail.

Слабые стороны:

- больше кальки;
- хуже литературный русский;
- нестабильность;
- mixed-script artifacts;
- незавершённые переводы.

В слепом тесте встречались, например:

```text
显然
lingering
```

Вывод:

> **Qwen выглядит существенно убедительнее как semantic verifier, чем как основной fiction translator.**

---

## T1 — Local T-lite

T-lite оказался явным аутсайдером.

Примеры ошибок:

```text
property → свойство
watercolor tattoos → водяные татуировки
weasel → мышка
deacon's bench → стул дека
```

Также были:

- необработанный английский;
- сломанный русский синтаксис;
- фактические подмены;
- повреждённые реплики.

Вывод:

> **T-lite пока не проходит минимальный quality bar для роли основного литературного translator.**

---

## Главный вывод первой части

Quality ladder на одинаковом фрагменте:

```text
DeepSeek Flash High     ≈ 8.3
Local Gemma reasoning 0 ≈ 8.0–8.1
DeepSeek Flash 0        ≈ 7.4
Local Qwen 0            ≈ 6.7
Local T-lite            ≈ 5.0
```

Два главных результата:

1. **Reasoning очень важен для DeepSeek.**
2. **Gemma reasoning=0 сама по себе очень сильна и почти достигает DeepSeek High.**

---

# Часть II. Independent №1 vs Pipeline Remote Reasoning v4.1

## Что такое Pipeline Remote Reasoning v4.1

Новый `translations.json` фиксируется под названием:

# **Pipeline Remote Reasoning v4.1**

Конфигурация:

- translator: **DeepSeek Flash**
- reasoning: **High**
- используется translation-часть Pact pipeline
- **без repair**
- **без audit**
- оценивается raw translation output до последующих проверок/repair

Это гораздо более чистый эксперимент, чем прежнее сравнение с полным pipeline.

Фактически сравниваются:

```text
DeepSeek High
+ short independent literary prompt
```

против:

```text
DeepSeek High
+ translation-stage pipeline instructions/context
```

---

## Итоговое сравнение

| Критерий | Independent №1 | Pipeline Remote Reasoning v4.1 |
|---|---:|---:|
| Точность смысла | **8.2–8.3** | 7.9 |
| Естественность русского | **8.3** | 7.6 |
| Связность | **8.4** | 8.0 |
| Голос Блейка | **8.5** | 7.8 |
| Диалоги | **8.5** | 7.8 |
| Регистр / мат | **8.7** | 7.8 |
| Сохранение деталей | 8.0 | **8.2** |
| Anti-calque | **8.4** | 7.5 |
| **Общее** | **≈8.3** | **≈7.9** |

Разрыв:

```text
≈0.4 балла
```

в пользу Independent №1.

---

## Что Pipeline Remote Reasoning v4.1 делает хорошо

Он:

- хорошо понимает сюжет;
- стабилен на длинном контексте;
- сохраняет structure;
- хорошо держит сцену с бабушкой;
- не разваливается на supernatural material;
- уверенно использует грубую лексику;
- иногда точнее сохраняет мелкие детали.

Например:

```text
weasel → ласка
No parking → Стоянка запрещена
```

---

## Где Independent №1 лучше

Главная разница — литературность.

### Пример: `lingering impressions`

Pipeline Remote Reasoning v4.1:

```text
Мои затяжные впечатления о доме вскоре развеялись.
```

Independent №1:

```text
Смутные впечатления о доме скоро развеялись.
```

Independent лучше переводит мысль, а не английскую конструкцию.

### Пример: `made it this far`

Pipeline Remote Reasoning v4.1:

```text
я прошёл так далеко
```

Independent №1:

```text
я дожил до этого момента
```

Снова Independent лучше выбирает контекстное русское значение.

---

## High reasoning значительно улучшил pipeline

Старое положение выглядело примерно так:

```text
Independent High ≈8.3
Pipeline Zero    ≈7.0
```

После включения High reasoning:

```text
Pipeline Remote Reasoning v4.1 ≈7.9
```

То есть большая часть прежнего разрыва действительно могла быть связана с reasoning=0 у DeepSeek translation pass.

---

## Но оставшийся translation-stage penalty сохраняется

Теперь модель и reasoning совпадают:

```text
Independent №1
DeepSeek Flash
High reasoning
≈8.3
```

```text
Pipeline Remote Reasoning v4.1
DeepSeek Flash
High reasoning
≈7.9
```

И audit/repair не участвовали.

Следовательно, оставшуюся разницу вероятнее искать в:

- translation prompt;
- glossary/context injection;
- bible;
- segmentation/chunking, если используется;
- других translation-stage constraints.

То есть:

> **Repair и audit не нужны, чтобы объяснить оставшийся ≈0.4 gap — деградация уже наблюдается до них.**

---

## Характер оставшейся деградации

Pipeline v4.1 чаще:

- сохраняет английскую конструкцию;
- выбирает более буквальный idiom mapping;
- звучит менее естественно;
- чуть хуже держит voice;
- иногда слишком сильно или слишком буквально передаёт мат.

Например `cunt → пизда` местами семантически объяснимо, но по-русски звучит менее естественно и менее точно по функции оскорбления, чем более контекстный вариант Independent №1.

---

## Consistency errors всё ещё возможны

В Pipeline Remote Reasoning v4.1 была заметная ошибка:

```text
motorcycle → велосипед
```

в нескольких местах.

Это показывает:

> **High reasoning не устраняет потребность в deterministic consistency QA.**

---

# Обновлённая экспериментальная картина

Теперь есть четыре важные DeepSeek-точки:

```text
DeepSeek High independent       ≈8.3
DeepSeek Zero independent       ≈7.4
DeepSeek High pipeline v4.1     ≈7.9
DeepSeek Zero full pipeline     ≈7.0
```

Это позволяет разделить два эффекта.

## Reasoning effect

На direct translation:

```text
High ≈8.3
Zero ≈7.4
```

Разница большая.

## Translation-stage pipeline effect

При одинаковом High reasoning:

```text
Independent High ≈8.3
Pipeline High    ≈7.9
```

Оставшийся gap существенно меньше:

```text
≈ -0.4
```

Это намного более благоприятная оценка pipeline, чем прежнее впечатление о `≈ -1.1`.

---

# Новый рабочий диагноз

Наиболее правдоподобная картина сейчас:

```text
Главный источник прежней деградации:
DeepSeek reasoning = 0

Вторичный источник:
translation-stage context / prompt / constraints

Repair и audit:
ещё требуют отдельного A/B-теста
```

---

# Следующие рекомендуемые эксперименты

## 1. Independent High + locked glossary only

Базовый prompt Independent №1 плюс только обязательные established terms.

Цель:

> проверить, можно ли получить consistency практически без literary penalty.

## 2. Independent High + glossary + compact bible

Добавить только cross-chapter context, который нельзя вывести из текущей главы.

Если качество падает именно здесь, значит context injection начинает конкурировать с literary task.

## 3. Current Pipeline Remote Reasoning v4.1

Использовать как текущую comparison point:

```text
≈7.9
```

## 4. Repair A/B

Сравнить:

```text
Pipeline Remote Reasoning v4.1
```

с:

```text
тот же translation output
+ repair
```

и измерять:

```text
repair precision =
repairs that genuinely improve translation
/
all repairs
```

---

# Отдельный приоритет: reasoning у локальной Gemma

Gemma reasoning=0 уже:

```text
≈8.0–8.1
```

Поэтому следующий локальный benchmark очень перспективен:

```text
Gemma 0
Gemma 2048
Gemma 4096
Gemma 8192
```

на одном и том же фрагменте.

Если reasoning даст даже небольшой прирост, локальная Gemma может сравняться с DeepSeek High.

---

# Текущая общая quality ladder

С учётом разных экспериментов:

```text
DeepSeek Flash High independent        ≈8.3
Local Gemma reasoning 0                ≈8.0–8.1
Pipeline Remote Reasoning v4.1         ≈7.9
DeepSeek Flash reasoning 0 independent ≈7.4
Local Qwen reasoning 0                 ≈6.7
Local T-lite                           ≈5.0
```

Важно: это не абсолютно единая лабораторная шкала, потому что часть оценок получена на первых ~110 PID, а Pipeline Remote Reasoning v4.1 дополнительно оценивался на гораздо большем диапазоне главы.

Самое чистое прямое сравнение пяти моделей/режимов — первая часть отчёта.

---

# Финальный вывод

## По моделям

- **DeepSeek Flash + High reasoning** остаётся лучшим абсолютным literary baseline.
- **Gemma reasoning=0** — лучший локальный translator и почти догоняет DeepSeek High.
- **Qwen reasoning=0** гораздо логичнее использовать как semantic verifier.
- **T-lite** пока слишком слаб для основного художественного перевода.

## По pipeline

Pipeline Remote Reasoning v4.1 значительно лучше старого reasoning=0 pipeline.

Включение High reasoning сократило прежний огромный разрыв с Independent №1 до примерно:

```text
≈0.4 балла
```

Но даже до audit/repair translation-stage pipeline всё ещё немного ухудшает литературную естественность относительно простого DeepSeek High whole-chapter call.

Наиболее вероятные источники оставшегося gap:

- prompt;
- context injection;
- glossary/bible constraints;
- segmentation;
- другие translation-stage instructions.

Главный следующий шаг:

> **Не добавлять новые стадии, а провести controlled ablation именно translation-stage context и отдельно измерить влияние repair.**
