## 0. Согласование

- [x] 0.1 Владелец утверждает изменение локальной границы book_state: посторонние объекты игнорируются, строгий exact-four остаётся для staging и кандидата Media. До утверждения не менять production-код. (Утверждено владельцем: последовательная разработка двух OpenSpec; simplify-book-formatting APPROVED и завершён независимо; это второй пункт последовательности, реализация разрешена.)

## 1. Локальное обновление

- [x] 1.1 Заменить проверку _validate_exact_four_file_set в MemoryManager.promote/_transactional_replace на проверку четырёх явных канонических путей без перечисления рабочего корня. Сохранить проверку JSON, regular-file, отсутствия symlink через всю цепочку предков и повторную проверку на границах транзакции. (Готово: _validate_canonical_selected_paths + O_NOFOLLOW/O_NONBLOCK чтение; alias _validate_exact_four_file_set сохранён.)
- [x] 1.2 Проверять только известные marker/backup пути при использовании, не доверять повреждённым или подменённым служебным объектам; сохранить pre/post hash, восстановление после прерывания и строгий exact-four внутри приватной staging-папки. (Готово: trust-checks до I/O, confinement backup в рабочий корень, fail-closed.)
- [x] 1.3 Обновить локальные тесты: посторонние top-level файл/директория, chapter_memory.json, symlink и FIFO разрешены и остаются неизменными; отсутствие/неверный JSON/не-regular или symlink у канонического имени отклоняются. (Готово: test_transaction_boundary, test_boundary_strictness, role_views + новый test_canonical_only_sync.py.)

## 2. Fetch и отправка

- [x] 2.1 Проверить оба пути fetch-current (SSH/tar и local facade) и injected transport: весь полученный пакет валидируется до применения; обновляются только четыре канонических файла и CURRENT.json/manifest.json, без обхода или очистки рабочего корня и без следования по symlink назначения. (Готово: _safe_replace_bytes с lstat-гейтом и атомарной заменой.)
- [x] 2.2 Зафиксировать один проверенный четырёхфайловый набор байтов для push_candidate. Из него вычислять manifest hash/size и tar members во всех transport-вариантах. Не читать посторонние файлы; не включать CURRENT.json или локальный manifest.json в candidate. (Готово: _pin_canonical_bytes + _pinned_state_files + _build_candidate_tar_bytes_from_pinned.)
- [x] 2.3 Сохранить подтверждение revision_id со стороны Media, ограниченный STALE_PARENT re-pull/retry и сохранение четырёх RT-обновлённых файлов; посторонние объекты не трогать. (Готово: retry-путь с symlink-проверкой restore; тесты подтверждают.)

## 3. Граничные проверки

- [x] 3.1 Положительная матрица: локальный promote и push при extra JSON, директории, symlink, FIFO, chapter_memory.json; после операции проверить неизменность этих объектов и точный список членов отправленного пакета. (Готово: 27 тестов в test_canonical_only_sync.py, включая preservation, точный fetch allow-list и D5 mutation-after-pin по всем transport-путям.)
- [x] 3.2 Отрицательная матрица для выбранного входа: неверный тип корня; symlink на каждом уровне предков; отсутствие, symlink, каталог, FIFO/socket/device и неверный JSON у каждого канонического имени; небезопасные CURRENT/manifest/marker/backup; подмена после precheck и после фиксации байтов. (Готово: остальные типы/уровни покрыты; device node отдельно не создавался, так как mknod требует недоступных привилегий; regular-file gate закрывает этот тип. См. residual risks.)
- [x] 3.3 Проверить manifest hash/size против реальных tar bytes, отказ Media на лишний/недостающий member и повреждённые данные, отсутствие Media push после неудачного локального promote, отказ без ACCEPTED при transport failure и STALE_PARENT retry. (Готово.)
- [x] 3.4 Провести целевые тесты MemoryManager, remote_client, run_hooks и snapshot receive/promote, затем независимое pact-dev/pact-rev ревью и OpenSpec validation. Живой pipeline не запускать. (Готово: целевые тесты прошли; pact-rev APPROVED; strict OpenSpec validation valid.)

## 4. Передача результата

- [x] 4.1 Сообщить изменённые файлы, результаты граничной матрицы и тестов, поведение на реальном составе book_state без изменения его содержимого, оставшиеся риски: READY FOR USER REVIEW. Merge, deployment и pipeline run — отдельные решения владельца. (Готово: handoff ниже; live RT/book_state не инспектировался, pipeline не запускался.)
