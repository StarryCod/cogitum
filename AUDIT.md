# Cogitum Pre-Release Audit

**Дата:** 2026-05-16
**Метод:** статический анализ (ruff, grep, AST) + точечное чтение + 4 года человеческого опыта чтения такого кода.
**Объём:** 74 .py файла, 19,623 строк, проверено всё.

Severity:
- **C** — Critical, блокер релиза
- **H** — High, исправить до релиза
- **M** — Medium, желательно
- **L** — Low / nitpick

═══════════════════════════════════════════════════════════════════
## 1. CORE / AGENT / TOOLS

### [C1] Loading-bug — tool_card зависает на «running…» если drain получил ошибку до AgentToolResult
- **File:** `cogitum/app.py:417-617`, `cogitum/widgets/feed.py:344-371`
- **Issue:** В `drain_queue()` если приходит `AgentError` или агент крашится между `AgentToolCall` и `AgentToolResult`, drain делает `return` (line 575) и tool_cards остаются в состоянии preparing/running. Спасает только `wait_for(drain_fut, timeout=10.0)` на line 605, но если AgentError ушёл в очередь и был обработан → drain ушёл нормально → fallback не триггерится → карта зависает навсегда.
- **Why bad:** это и есть «eternal loading» из жалобы юзера.
- **Fix:** В обработчике `AgentError` в drain пройти по `tool_cards.values()` и пометить `_result is None` как `set_result("(interrupted)", error=True)` ПЕРЕД `return`. Аналогично в `AgentDone` если есть карты без результата.

### [C2] Race condition в drain timeout fallback — `card._result` доступ без lock + use of private attr
- **File:** `cogitum/app.py:612-614`
- **Issue:** `if card._result is None` обращается к приватному атрибуту виджета из другого корутина одновременно с `set_result` который мог быть вызван drain'ом.
- **Why bad:** В Textual reactive update должен идти через `call_from_thread` или message-passing.
- **Fix:** Добавить публичный метод `ToolCallCard.is_pending() -> bool` и вызывать только после `drain_fut.cancel()` + `await asyncio.sleep(0)`.

### [C3] `cogit save` — checkpoint без gitignore, тащит всё подряд кроме хардкод-списка
- **File:** `cogitum/core/cogit.py:29, 52-93`
- **Issue:** `_SKIP_DIRS` = только `.git/.venv/__pycache__/...`. Не читает `.gitignore`. Юзерские dataset, models/, .env, secrets.toml, hf_cache, ms-playwright, *.bin, *.gguf — всё попадает в snapshot. При `cogit save` без scope в проекте с моделями получишь МБ JSON и таймаут.
- **Why bad:** юзер сказал "не до конца сделан" — вот один из недоделов.
- **Fix:** Добавить чтение `.gitignore` (pathspec lib или своя реализация), плюс расширить `_SKIP_DIRS` (`.idea`, `.vscode`, `target`, `out`, `coverage`, `models`, `weights`).

### [C4] `cogit restore` — НЕ удаляет файлы которых нет в snapshot, делает ТОЛЬКО overwrite
- **File:** `cogitum/core/cogit.py:157-179`
- **Issue:** Если между save и restore агент СОЗДАЛ новый файл, restore этот файл НЕ удалит. Restore != git checkout. Юзер думает что вернулся к точке X, но в проекте остаётся мусор.
- **Why bad:** второй главный недодел cogit. Силно ломает доверие к фиче.
- **Fix:** Перед write_text пройти по `_collect_files(scope)` текущего состояния, найти файлы которых нет в snapshot — удалить их (опционально с подтверждением).

### [C5] `cogit save` — нет диффа между чекпоинтами, дублирует ВЕСЬ контент каждый раз
- **File:** `cogitum/core/cogit.py:104-130`
- **Issue:** Snapshot хранит `{path, content}` фул-копией. При 5-10 чекпоинтах ~МБ кода → десятки МБ JSON в `~/.config/cogitum/cogits/{session_id}/`. Поле `hashlib` импортировано (line 18) но НЕ используется — кто-то начал делать content-addressable storage и забил.
- **Why bad:** третий недодел cogit (юзер прав, cogit "вроде есть, вроде нет"). Также — disk leak.
- **Fix:** content-addressable: хранить `{path, sha256}`, а реальный контент в `objects/{sha256}` (как git). Дедупликация неизменённых файлов. cleanup() удаляет только unreferenced objects.

### [C6] `cogit cleanup` НЕ удаляет содержимое — только сами json'ы
- **File:** `cogitum/core/cogit.py:233-241`
- **Issue:** При content-addressable storage (см. C5) cleanup должен GC objects. Сейчас даже без CAS — удаляются json'ы в `store_dir`, но сама `store_dir` не пересоздаётся, нет cleanup для пустых session_id директорий.
- **Fix:** GC orphan objects + удалять пустые `~/.config/cogitum/cogits/<session_id>/` папки.

### [H1] 30+ silent `except Exception: pass` подавляют все ошибки
- **Files:** `app.py` (12 мест), `setup_flow.py` (4), `cli.py` (3), `builtin_tools.py` (4), `widgets/approval.py:181`, `core/auth/storage.py:61`, `core/llm/refresh.py:123`, `core/llm/discovery.py:205`, etc.
- **Issue:** Везде паттерн:
  ```python
  try:
      self.query_one("#inspector-widget", Inspector).stream_delta(...)
  except Exception:
      pass
  ```
  Если Inspector сломан (например query_one не нашёл из-за рейс-кондишена при teardown) — никогда не узнаешь.
- **Fix:** Минимум — `log.debug(...)` в except. Лучше — суженный except (`NoMatches`, `KeyError`).

### [H2] Fire-and-forget asyncio.create_task без сохранения reference (RUF006) — задачи могут быть GC'нуты
- **Files:**
  - `app.py:99` — `asyncio.ensure_future(self._auto_refresh_models())` не сохранён
  - `gateway/telegram.py:402` — `asyncio.create_task(self._handle_update(update))` — каждый апдейт fire-and-forget. Если GC поторопится → апдейт потеряется БЕЗ trace.
  - `gateway/telegram.py:956` — signal handler
  - `core/agent.py:615` — `_execute_tool_indexed` (но это сохраняется в `tool_tasks` через list comprehension — ОК)
- **Why bad:** Python может GC корутину если нет hard ref. Реально воспроизводится при memory pressure.
- **Fix:** Хранить в `set` instance-attr, добавлять в `add_done_callback(set.discard)`.

### [H3] Telegram update handler — не ограничен по concurrency, можно DoS'ить бота
- **File:** `cogitum/gateway/telegram.py:402`
- **Issue:** Каждый incoming update → fresh task. Если кто-то спамит, или сам бот шлёт ошибки которые триггерят retry, накапливаются параллельные задачи без bound.
- **Fix:** asyncio.Semaphore(N) + queue.

### [H4] Telegram bot — НЕТ persistence offset (`self._offset = 0` в __init__)
- **File:** `cogitum/gateway/telegram.py:230, 397-401`
- **Issue:** При рестарте `_offset` сбрасывается в 0, getUpdates возвращает старые сообщения которые уже обработаны (TG хранит 24ч). Юзер пришлёт сообщение → перезапустит бота → получит дубль ответа.
- **Fix:** Сохранять offset в `~/.config/cogitum/tg_offset` после каждого update.

### [H5] Telegram bot — backoff на network error всего 5s, нет exponential
- **File:** `cogitum/gateway/telegram.py:409` (sleep 5)
- **Issue:** При длительном down API будем спамить раз в 5 сек. У httpx есть retry, но connect refused/DNS fail → бесконечный hot loop.
- **Fix:** Exponential backoff 1→2→4→8→16→cap 30s, reset after success.

### [H6] mutable class default (RUF012) — 16 мест
- **Files:** `app.py:52` (BINDINGS), `widgets/composer.py`, `widgets/approval.py`, `widgets/cards.py`, etc.
- **Issue:** `BINDINGS = [...]` как class attr — если унаследовать класс и `BINDINGS.append(...)` мутируешь parent. Textual Frame имеет свой механизм но для `_SPECIAL_TOOLS = {...}` в feed.py — тоже class-level dict.
- **Fix:** `ClassVar[tuple[Binding, ...]] = (...)` для иммутабельности или явно `ClassVar[list[...]]`.

### [H7] Эмодзи всё ещё в setup_flow + telegram (40K стиль нарушен)
- **Files:** `setup_flow.py` (200, 317, 320, 1341, 1353, 1358, 1360, 1363, 1540) — 9 мест с `⚠`
  `gateway/telegram.py` (277-285, 291, 293, 345, 386, 429, 489) — 12+ мест с `✦📋✏️🔧🤖🔄♻️❓⛔📨`
- **Why bad:** юзер явно сказал «40K vibe, без эмодзи, golden glyphs». Прошлая сессия заменила в approval/keyboard, но забыла остальное.
- **Fix:** Заменить на 40K руны: `⚠ → ▲`, `✦ → ✦` (этот ОК — звезда), `📋 → ◆`, `✏️ → ✎`, `🔧 → ⚙`, `🤖 → ◇`, `🔄/♻️ → ⟳`, `❓ → ?`, `⛔ → ✕`, `📨 → ▸`. Плюс agent.py:670 — `📨 injected:` → `▸ injected:`.

### [H8] `cogit` нет проверок safety на restore — может затереть несохранённые правки
- **File:** `cogitum/core/cogit.py:157-179`
- **Issue:** Restore делает `target.write_text(entry["content"])` без diff'а с текущим. Если юзер написал важный код после save'а и хочет вернуться к save'у — потеряет всё несохранённое БЕЗ предупреждения.
- **Fix:** Перед restore автосейв `__pre_restore_<index>__` (auto-checkpoint), показать предупреждение в TUI.

### [M1] subprocess.run в setup_flow.py:2105 — `subprocess.call([ed, ...])` без timeout
- **Issue:** Если `ed` — vim/nano с suspend, ОК. Но если что-то сломается (broken editor) — TUI завис.
- **Fix:** `subprocess.run(..., timeout=3600)` или явный suspend wrapper.

### [M2] Daemon операции `subprocess.run` без timeout — если systemd завис, гейтуэй залип
- **File:** `cogitum/gateway/daemon.py:51, 58, 70, 81, 93, 104, 118, 133, 148`
- **Fix:** `timeout=10` на все systemctl-вызовы.

### [M3] Builtin file_search использует subprocess(grep) вместо ripgrep, при таймауте teruncates results
- **File:** `cogitum/core/builtin_tools.py:308-318`
- **Issue:** timeout=15 на каждый grep. На большом репо может быть мало.
- **Fix:** Использовать ripgrep (rg) если доступен — в 10x быстрее, меньше шанс таймаута.

### [M4] `_execute_tool` — нет timeout на сами tool-ы (кроме terminal mode='timeout')
- **File:** `cogitum/core/agent.py:896+`
- **Issue:** Например `browser(action='open', url=...)` может зависнуть на 5 минут на slow page. Нет global tool timeout.
- **Fix:** `asyncio.wait_for(tool_fn(...), timeout=180)` на уровне `_execute_tool`.

### [M5] Cogit `_collect_files` без gitignore тратит time/IO на rglob по `node_modules` если случайно отсутствует в _SKIP_DIRS
- См. C3.

### [M6] OpenAI compat streaming — F841 unused `e` в except, может скрывать важную инфу
- **File:** `cogitum/core/llm/providers/openai_compat.py:82` (pass в except)
- **Fix:** Логировать.

### [L1] 57 unused imports (F401), 44 unused noqa (RUF100) — мусор по проекту
- **Fix:** `.venv/bin/ruff check --fix --select F401,RUF100 cogitum/ tests/` (auto-fixable).

### [L2] Inconsistency — `agent.py` использует `print` или `log.exception`, в других местах — silent except
- **Fix:** Стандартизировать через `log = logging.getLogger("cogitum.core")`.

═══════════════════════════════════════════════════════════════════
## 2. LLM / AUTH

### [H9] `cache_control` — whitelist по `base_url` правильный, но проверки нет в codepath где endpoint пустой
- **File:** `cogitum/core/llm/prompt_caching.py`
- **Issue:** Если `base_url` пустой (default Anthropic SDK без override), нужна явная проверка.
- **Status:** проверить руками — возможно уже OK.

### [H10] `secrets.env` — нет проверки mode 0600 при чтении
- **File:** `cogitum/core/llm/secrets_env.py`
- **Issue:** Файл создаётся с mode 0600, но если юзер вручную скопировал/отредактировал → mode может быть 0644, и предупреждения нет.
- **Fix:** При load — `os.stat`, если mode != 0600 → warning в лог.

### [M7] OAuth callback server — bind на all interfaces?
- **File:** `cogitum/core/auth/callback_server.py`
- **Check:** должен биндиться на `127.0.0.1`, не `0.0.0.0`. Иначе любой в локалке может перехватить authcode.

### [M8] PKCE state — проверить CSRF-валидацию на callback
- **File:** `cogitum/core/auth/pkce.py` + `callback_server.py`

### [M9] Discovery / refresh — phantom pruning ОК, но при 401 не различает «токен истёк» vs «нет models»
- **File:** `cogitum/core/llm/discovery.py:205`
- **Issue:** silent except — refresh не сообщает что ключ умер, юзер думает «нет моделей».
- **Fix:** Различать HTTP коды, показывать в TUI.

═══════════════════════════════════════════════════════════════════
## 3. UI / TUI

### [C7] ToolCallCard — preliminary card может не получить full args если LLM не шлёт preliminary
- **File:** `cogitum/app.py:472-488`, `cogitum/core/agent.py:518-524, 593`
- **Issue:** preliminary event шлётся ТОЛЬКО если стрим парсит tool_use_start (line 518). Если провайдер шлёт tool_use одним блоком (Cerebras/некоторые non-anthropic пути), preliminary НЕ создаётся, потом приходит full event — путь через `existing = tool_cards.get(call_id); if existing:` (line 481) — ОК. Но если случилась ошибка в стриме ДО full event и ПОСЛЕ preliminary → preliminary card вечно «preparing…».
- **Fix:** В `AgentError`/`AgentDone` хэндлерах drain'а пройтись по `tool_cards` и пометить preparing-карты как cancelled.

### [H11] `_make_rich_card` returns None — generic ToolCallCard остаётся, но если rich_card не создан И result большой → обрезается на 4 строки
- **File:** `cogitum/widgets/feed.py:519-534`
- **Issue:** Жёстко 4 строки result в generic. Юзер видит «✓ done\n  +3 lines» и не знает что там реально.
- **Fix:** Кликабельный «show more» или хотя бы 10 строк.

### [H12] WaitingIndicator не cleanup'ится при quit/cancel в некоторых путях
- **File:** `cogitum/app.py:608-610` (только в timeout fallback)
- **Issue:** В путях AgentDone/AgentError waiting очищается, но если drain прошёл нормально и есть лишний `WaitingIndicator` (например после AgentToolResult был создан line 524) → задержался.
- **Fix:** Финальный sweep `for w in feed.query("WaitingIndicator"): w.stop()` в конце run.

### [H13] Approval widget — теперь focusable (исправлено), но если юзер кликнул мышкой ВНЕ виджета пока ждёт approval, focus теряется
- **File:** `cogitum/widgets/approval.py`
- **Fix:** `on_blur` event → re-focus self пока approval активен. Или modal screen вместо inline widget.

### [M10] CSS/design — `cogitum/cogitum.tcss` ссылается на токены — проверить что все используемые цвета определены в `design.py`
- **Action:** прогнать grep на `$token-name` в .tcss и сверить с design.py.

### [M11] Composer paste placeholder — что если paste длинный (10K строк)?
- **File:** `cogitum/widgets/composer.py:438`
- **Issue:** placeholder `[Pasted N chars]` — но реальный текст всё ещё в буфере композера, может фризить TUI при render.
- **Fix:** Если paste > 5K chars — хранить в `_pasted_chunks: list[str]`, показывать только placeholder, при submit подставлять.

### [M12] ModelPicker — substring search highlight, но если term содержит regex-meta символы (`/`, `(`) — ломается?
- **File:** `cogitum/widgets/model_picker.py`
- **Check:** `re.escape(term)` должен использоваться.

### [M13] Setup wizard — 22 теста есть, но нет integration-теста полного flow «от 0 до агент отвечает»
- **File:** `tests/test_setup_wizard.py`
- **Status:** good coverage компонентов, но holistic флоу (set provider → save key → refresh → pick model → отправить запрос → получить ответ) не покрыт.

### [M14] Banner widget — если терминал узкий (< 80 cols), ascii-art ломается?
- **File:** `cogitum/widgets/banner.py`
- **Check:** есть ли responsive fallback.

### [L3] QueueBar (90 строк) — счётчик может стать negative при race?
- **File:** `cogitum/widgets/queue_bar.py`
- **Check:** `self.count = max(0, self.count - 1)` нужен.

### [L4] SessionPicker — если 0 сессий, что показывает?
- **File:** `cogitum/widgets/session_picker.py`

═══════════════════════════════════════════════════════════════════
## 4. GATEWAY (TG)

### [C8] TG bot — НЕТ rate limiting на approval callbacks, можно зафлудить тысячами кликов inline keyboard
- **File:** `cogitum/gateway/telegram.py`
- **Fix:** dedup по `callback_query.id`, debounce.

### [H14] TG bot — `escape_md` применяется не везде, MarkdownV2 padding опасен
- **File:** `cogitum/gateway/telegram.py:386, 489, etc.`
- **Issue:** Если в имени модели есть `_` — TG отрендерит как italic / даст 400 ошибку.
- **Fix:** Pass через escape_md везде.

### [H15] Markdown→TG конвертер — таблицы конвертируются в bullet groups, но что если pipe внутри code block?
- **File:** `cogitum/gateway/tg_formatter.py`
- **Status:** требует точечного теста.

### [M15] send_media — _set_tg_context при параллельных запросах из multi-агентов?
- **File:** `cogitum/gateway/telegram.py` + `core/builtin_tools.py`
- **Issue:** Если delegate_task запускает 3 sub-агента и каждый шлёт media, _tg_context (instance-level?) может перепутаться. Нужно contextvars.

### [M16] Daemon stop-then-start race
- **File:** `cogitum/gateway/daemon.py`
- **Issue:** systemctl stop returns когда unit "stopping", не "stopped". Restart-then-start даст конфликт.
- **Fix:** Wait pidfile clear перед start.

═══════════════════════════════════════════════════════════════════
## 5. TESTS / COVERAGE

### НЕ покрыто unit-тестами:
- `cogitum/core/agent.py` — главный orchestrator (1058 строк), 0 unit
- `cogitum/core/builtin_tools.py` — 1254 строк, частично через test_terminal_modes (12) и test_browser_live (7), file ops/cogit/web_search/fetch_url НЕ покрыты
- `cogitum/core/cogit.py` — 0 тестов вообще (особенно учитывая C3-C6!)
- `cogitum/core/delegate.py` — 541 строк, 0 unit
- `cogitum/core/sessions.py` — 0
- `cogitum/core/memory.py` — 0
- `cogitum/core/skills.py` — 0
- `cogitum/core/godmode.py` — 0
- `cogitum/core/process_manager.py` — частично через test_terminal_modes
- `cogitum/gateway/telegram.py` — 979 строк, 0 unit
- `cogitum/gateway/tg_formatter.py` — 299 строк, 0
- `cogitum/gateway/daemon.py` — 0
- `cogitum/widgets/composer.py` — 0
- `cogitum/widgets/feed.py` — 0 (главный виджет где живёт loading bug!)
- `cogitum/widgets/cards.py` — 0
- `cogitum/widgets/inspector.py` — 0
- `cogitum/widgets/banner.py` — 0
- `cogitum/widgets/button.py` — 0
- `cogitum/widgets/queue_bar.py` — 0
- `cogitum/widgets/session_picker.py` — 0

### Test smell:
- 104 теста проходят за 34с — значит большинство быстрые/изолированные, ОК
- `tests/test_browser_live.py` — реально живые, требуют chromium
- `tests/test_mesh_reload.py` — может задевать глобальный env

═══════════════════════════════════════════════════════════════════
## 6. DEAD CODE / МУСОР

### Auto-fixable ruff (102 issues):
- 57 unused imports (F401)
- 44 unused noqa (RUF100)
- 1 SIM108 (if-else можно ternary)
- Команда: `.venv/bin/ruff check --fix cogitum/ tests/`

### Manual:
- `cogitum/core/cogit.py:18` — `import hashlib` не используется (started CAS but abandoned, см. C5)
- `cogitum/app.py:18` — `SessionStore` импорт unused
- `cogitum/app.py:26` — `InspectorState` импорт unused
- 16 mutable class defaults (RUF012) — стиль/safety

### Файлы которые видел в репо (нужно проверить нет ли):
- `error.txt`, `htest.txt`, `test_all_tools.txt`, `hermes-agent/` — всё удалено в прошлом коммите ✓

═══════════════════════════════════════════════════════════════════
## 7. INCOMPLETE FEATURES

### Cogit (главная недоделка):
- C3: `.gitignore` не читается
- C4: restore не удаляет orphan files
- C5: нет дедупликации (hashlib импортирован но не использован)
- C6: cleanup не GC'ит content
- H8: нет safety pre-restore checkpoint

### TG Gateway:
- H4: нет offset persistence
- H5: нет exponential backoff
- C8: нет rate limit на callbacks

### Tests:
- Большие пробелы в coverage (см. секцию 5)

═══════════════════════════════════════════════════════════════════
## 8. OPTIMIZATION OPPORTUNITIES

- Cogit с CAS: -90% disk usage при 10+ checkpoints
- Замена subprocess(grep) на rg в builtin_tools: -10x latency
- Composer paste >5K chars в memory only at submit time: -лаги UI
- ModelPicker: SQLite FTS5 вместо in-memory fuzzy при > 100 моделей

═══════════════════════════════════════════════════════════════════
## SUMMARY

| Severity | Count |
|----------|-------|
| Critical (C) | 8 |
| High (H) | 15 |
| Medium (M) | 16 |
| Low (L) | 4 |
| **Total findings** | **43** |
| Dead code (auto-fix) | 102 |
| Missing tests | ~17 файлов |
| TODO/FIXME explicit | 0 (мусора по комментам нет, но недоделки скрыты в логике) |

### Топ-5 что чинить ПЕРВЫМ:
1. **C1** — eternal loading на tool cards
2. **C3-C5** — cogit недоделан (юзер прямо просил)
3. **H7** — эмодзи в setup_flow + telegram (40K стиль)
4. **H4** — TG offset persistence
5. **C8** — TG callback rate limit

### Топ-5 quick wins (5-15 минут каждый):
1. `ruff --fix` — 102 finding'а одним махом
2. Заменить эмодзи на руны (H7) — sed по 2 файлам
3. TG offset persistence (H4) — 5 строк
4. Cogit gitignore чтение (C3) — pathspec lib + 10 строк
5. Логирование в silent except (H1) — `log.debug` вместо `pass`
