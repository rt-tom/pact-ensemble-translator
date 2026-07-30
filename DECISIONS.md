2026-07-27. Полная сверка веток репозитория. Все 40+ веток и 46 worktree проверены на уникальное содержимое относительно main — уникального нет. main (тег v3.1.3-hotfix.2) содержит полный состав v3.1.3: atomic artifacts, cache identity, artifact DAG, stage execution protocol, chapter manifest, chapter resolver, formatting integrity. Ветки заархивированы тегами archive/\*, рабочие копии сняты.



Хронология 26.07: фичи v3.1.3 вливались поштучно (b513767, da5d09a), откачены в 20:44 (d2c6b36, 2c44679), затем влиты целиком релизным squash-коммитом 231af93f. Откаты — не инцидент, а перестроение способа вливания.



Аудиты V313\_INDEPENDENT\_AUDIT\_FINDINGS.md и V313\_RC2\_DELTA\_REAUDIT.md писались против develop/v3.1.3, а не против main. К текущему коду применимы частично — требуют перепроверки перед использованием как списка задач.

2026-07-28. V4 identity считается проверенной только после содержательной привязки к ожидаемым source/snapshot/authoritative chunk plan/config artifacts; один лишь SHA-256-shaped regex не отвергает foreign identity и недостаточен для cache/resume.

2026-07-30. Phase 1C → 2A → 2B → 2C driver: Phase 2B (generation) и Phase 2C (cascaded selection) выполняются **в одном per-chunk цикле**, не в двух отдельных проходах. Причина: chunk N+1's `left_context` (Russian text, used as read-only context in the generation prompt) должен браться только из cascade-выбранного кандидата chunk N, а не из `outcome.candidates[expected_roles[0]]` (это просто первый role из generation, до cascade'а). Cascade-контракт "no least-bad selection" применяется симметрично к context propagation: chunk, который не был selected (quarantined / needs_synthesis / incomplete_generation), не имеет established translation — подавать его `fidelity_first` draft в prompt следующего чанка = тихий fallback, который cascade отказывается делать на этапе selection. Альтернативы (pre-pass по selection, отдельный "selected_text_by_chunk" обновляемый после cascade) не дают той же инвариантности: pre-pass требует ещё одного прохода по chunk'ам, а двухфазный driver требует либо подавать неправильный текст, либо решать "что есть selected translation" до cascade'а. Per-chunk interleaving — единственный вариант, который держит инвариант "left_context = previous chunk's selected translation, или () если previous chunk не selected" буквально. Зафиксировано в `pact_v4/pipeline/v4_phase12_draft_runner.py:run_chapter` docstring.
