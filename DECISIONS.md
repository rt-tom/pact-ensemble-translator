2026-07-27. Полная сверка веток репозитория. Все 40+ веток и 46 worktree проверены на уникальное содержимое относительно main — уникального нет. main (тег v3.1.3-hotfix.2) содержит полный состав v3.1.3: atomic artifacts, cache identity, artifact DAG, stage execution protocol, chapter manifest, chapter resolver, formatting integrity. Ветки заархивированы тегами archive/\*, рабочие копии сняты.

2026-07-28. После последнего разрешённого ensemble repair round непринятые кандидаты больше не останавливают pipeline: сохраняется текущий перевод, а lifecycle получает явный терминальный статус `resolved_retry_exhausted`; причина остаётся видимой в quality-артефактах.



Хронология 26.07: фичи v3.1.3 вливались поштучно (b513767, da5d09a), откачены в 20:44 (d2c6b36, 2c44679), затем влиты целиком релизным squash-коммитом 231af93f. Откаты — не инцидент, а перестроение способа вливания.



Аудиты V313\_INDEPENDENT\_AUDIT\_FINDINGS.md и V313\_RC2\_DELTA\_REAUDIT.md писались против develop/v3.1.3, а не против main. К текущему коду применимы частично — требуют перепроверки перед использованием как списка задач.
2026-07-28. Translation resume may be declared `REUSED` before model startup only when every chunk in each current manifest has a draft cache containing all of that chunk's PIDs; this replaces the deliberately pessimistic always-`MODEL_REQUIRED` probe without trusting a single aggregate output file.

2026-07-29. Для завершения chapter-wide final Qwen smoke контекст увеличивается только в выделенном профиле `QwenGlobalSmoke` и только при явном параметре runner; остальные Qwen-этапы сохраняют 32K, чтобы не менять их проверенный runtime/cache профиль.

2026-07-29. После трёх невалидных JSON-ответов, каждый из которых достиг лимита 2600 токенов, бюджет вывода `qwen_global_smoke` повышен только для финального chapter-wide smoke до 5000; при контексте 40K это остаётся внутри доступного окна, а остальные Qwen-стадии не меняются.

2026-07-29. Отклонённые raw-ответы final Qwen global smoke сохраняются только в `v31/final/diagnostics/qwen_global_smoke` с `authoritative=false`: это позволяет видеть полный JSON и PID обрыва, но файлы не являются агрегатами и не участвуют в cache/reuse или качестве перевода.
