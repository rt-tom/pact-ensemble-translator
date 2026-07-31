2026-07-27. Полная сверка веток репозитория. Все 40+ веток и 46 worktree проверены на уникальное содержимое относительно main — уникального нет. main (тег v3.1.3-hotfix.2) содержит полный состав v3.1.3: atomic artifacts, cache identity, artifact DAG, stage execution protocol, chapter manifest, chapter resolver, formatting integrity. Ветки заархивированы тегами archive/\*, рабочие копии сняты.



Хронология 26.07: фичи v3.1.3 вливались поштучно (b513767, da5d09a), откачены в 20:44 (d2c6b36, 2c44679), затем влиты целиком релизным squash-коммитом 231af93f. Откаты — не инцидент, а перестроение способа вливания.



Аудиты V313\_INDEPENDENT\_AUDIT\_FINDINGS.md и V313\_RC2\_DELTA\_REAUDIT.md писались против develop/v3.1.3, а не против main. К текущему коду применимы частично — требуют перепроверки перед использованием как списка задач.



2026-07-30. ChunkPlan (pact_v4/phase1/models.py) переведён с PID-based hard cap (MIN_PIDS=8/MAX_PIDS=20) на word-based (MIN_WORDS=280/MAX_WORDS=640), это отменяет предыдущее решение зафиксировать 8-20 PID как структурный инвариант. Причина: docs/plans/V4_PHASE_0C_GATE_NOTE_RU.md §1 фиксирует small chunk profile Phase 0C Gate как word-based (target_words=450, min_words=280, max_words=640) и явно документирует, что тот же baseline-профиль на практике дал 16-32 PID/чанк (mean 25.21) — то есть PID-based cap=20 систематически конфликтует с собственным Gate-профилем. Правка PID-cap затрагивала models.py, хотя карточка Work 1 изначально ограничивала diff файлом pact_v4/phase1/chunker.py + тесты — расширение согласовано явно (не тихая правка вне заявленного scope). ChunkPlan получил обязательное поле total_words (сумма word_count исходных PID чанка, вычисляется в chunker.py, а не в модели — ChunkPlan сам не хранит текст source). pact_v4/phase1/chunker.py (ChunkPlanner) переведён на word-based partitioning: DEFAULT_TARGET_WORDS=450, DEFAULT_MIN_WORDS=280, DEFAULT_MAX_WORDS=640, DEFAULT_FOLLOWING_BLOCKS=0 (following_blocks — переименованный context_right_count, right context остаётся допустимой опцией, не default). Тай-брейк между равнозначными natural breaks сохранён как в PID-версии (приоритет наибольшему чанку, ближе к max_words), а не к target_words — target_words используется только как валидируемая граница профиля (min_words <= target_words <= max_words), не как эвристика выбора разрыва: это сознательно узкое решение, чтобы не менять поведение алгоритма сверх необходимого. docs/schemas/v4_chunkplan.schema.json обновлён синхронно (total_words — required integer, hard cap описан в словах, не в PID).

