2026-07-27. Полная сверка веток репозитория. Все 40+ веток и 46 worktree проверены на уникальное содержимое относительно main — уникального нет. main (тег v3.1.3-hotfix.2) содержит полный состав v3.1.3: atomic artifacts, cache identity, artifact DAG, stage execution protocol, chapter manifest, chapter resolver, formatting integrity. Ветки заархивированы тегами archive/\*, рабочие копии сняты.

2026-07-28. После последнего разрешённого ensemble repair round непринятые кандидаты больше не останавливают pipeline: сохраняется текущий перевод, а lifecycle получает явный терминальный статус `resolved_retry_exhausted`; причина остаётся видимой в quality-артефактах.



Хронология 26.07: фичи v3.1.3 вливались поштучно (b513767, da5d09a), откачены в 20:44 (d2c6b36, 2c44679), затем влиты целиком релизным squash-коммитом 231af93f. Откаты — не инцидент, а перестроение способа вливания.



Аудиты V313\_INDEPENDENT\_AUDIT\_FINDINGS.md и V313\_RC2\_DELTA\_REAUDIT.md писались против develop/v3.1.3, а не против main. К текущему коду применимы частично — требуют перепроверки перед использованием как списка задач.
2026-07-28. Translation resume may be declared `REUSED` before model startup only when every chunk in each current manifest has a draft cache containing all of that chunk's PIDs; this replaces the deliberately pessimistic always-`MODEL_REQUIRED` probe without trusting a single aggregate output file.

2026-07-28. V4 identity считается проверенной только после содержательной привязки к ожидаемым source/snapshot/authoritative chunk plan/config artifacts; один лишь SHA-256-shaped regex не отвергает foreign identity и недостаточен для cache/resume.

2026-07-28. Phase 0C baseline разделена на два независимых источника, не смешиваемых в одной метрике: Track A — chapter 046 / golden set (FP-candidate rate поверх 57 accepted PID), Track B — глава 100 / v3.1 production run (внутренние bad-repair/residual/integrity/time-tokens). needs_review записи (43) исключены из численных метрик Track A как ограничение неполной курации, не дизайн; semantic recall в этом раунде не измерим (known_violations пустые во всех 100 записях), суррогат не подставляется. Из вывода v3 главы 100 golden-record не строится (schema требует независимый human_translation_epub, которого для главы 100 нет; anchoring bias). Результат прогонов/тексты не коммитятся — только агрегированный versioned result record с хешами/метриками.

2026-07-29. Несовпадение production HEAD с commit/tag в deployment provenance — release drift, а не допустимый промежуточный state: active tree и runs сохраняются, reconciliation проходит через reviewed release path. Незавершённый V4 не включается в V3 deployment tree.
