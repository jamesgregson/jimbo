# Jimbo roadmap: what's missing, what's needed, in what order

## Context

Jimbo is at "milestone 1" (~1.2k lines): a headless `AgentSession` (pydantic-ai 2.63 `agent.iter`) emits
`Event`s to sinks, tools go through an `ApprovalGate` policy chain, and three frontends (plain/tui/web)
implement the `Frontend` protocol. The goal is to grow it into a usable daily coding agent: slash
commands, paste, steering, skills, compaction, planning, todos, plus the gaps found while reading the code.

### What exists (and should be reused)
- `core/session.py` `AgentSession.send()` — one turn, streams deltas/tool events, keeps `self.history`.
- `core/events.py` `EventKind` — single event vocabulary; new features add kinds here.
- `core/approval.py` policy chain — plan mode becomes just another policy.
- `core/deps.py` `Deps.authorize()` — single choke point every new tool must call.
- `core/runner.py` `run_frontend()` — the input loop; natural home for command dispatch.
- `wiring.py` `build_session()` — composition root; new subsystems are wired here.
- Tests: `tests/test_core.py` `ScriptedFrontend` + `FunctionModel`/`TestModel` pattern.

### What's missing (found in the code)
| Area | Gap |
|---|---|
| Run control | `send()` is awaited inline; no cancel/interrupt (Ctrl+C mid-run kills the process in TUI/plain), no input while a run is in flight |
| Input layer | Every input string goes straight to the model; no command parsing |
| Tools | No shell, grep, glob; no web fetch |
| Context | No context-window size per model, no token tracking across turns (`usage` is per run only), no compaction |
| Persistence | JSONL logs events, not `ModelMessage`s — sessions can't be resumed |
| Prompting | System prompt is a constant; no AGENTS.md/project instructions, no skills |
| Approvals | Only per-tool "always"; no pattern rules (e.g. allow `shell: git status*`), no config file |
| Input content | `send(text: str)` only — no images/files; no paste handling |
| Agentic features | No todos, plan mode, subagents, MCP |

## Proposed order

Ordering principle: build the two foundations everything else hangs on (**command dispatch** and
**run-as-a-task**), then the high-value tools, then features roughly by dependency.

### Phase 0 — Foundations (small, unblocks everything) — done
1. **Run as a cancellable task.** `run_frontend` runs `session.send()` in an `asyncio.Task`; add
   `session.cancel()`; on `CancelledError` keep history up to the last complete message pair and emit
   `run_finished(ok=False, cancelled=True)`. TUI: Esc/Ctrl+C during a run cancels instead of quitting.
   Web: "stop" button → `{"type":"cancel"}`. Needed by steering, and good on its own.
2. **Command registry in core.** `core/commands.py`: `Command(name, help, handler(session, args))`,
   `CommandRegistry.dispatch(text)` in `run_frontend` (and web's queue consumer) before `send()`.
   New event `command_output`. Frontends add tab-completion from `registry.names()` (prompt_toolkit
   `WordCompleter`; web: simple popup).
3. **Context accounting.** `Settings.context_window` (default from `num_ctx`, override per model);
   after each model request emit `context` event with last `input_tokens` / window. Feeds `/usage`,
   status line and auto-compaction.

### Phase 1 — Slash commands (first batch on the registry) — done
`/help`, `/clear` (reset history), `/model <p:m>` (rebuild agent), `/usage`, `/approve yolo|ask`,
`/quit`. Later phases register `/compact`, `/plan`, `/todos`, `/skills`, `/resume`.
Also: user-defined commands as markdown files in `~/.jimbo/commands/` and `.jimbo/commands/`
(file body = prompt template with `$ARGUMENTS`); same loader pattern reused by skills.

### Phase 2 — Shell and search tools — done
- `tools/search.py`: `glob(pattern)`, `grep(pattern, path, glob)` — read-only, use `rg` if present,
  else Python fallback; respect `.gitignore`; output caps like `fs.py`.
- `tools/shell.py`: `run(command, timeout)` via `asyncio.create_subprocess_shell` in the workspace,
  output capped/truncated, killed on timeout and on session cancel. Not read-only → asks.
- Approval rules: `PatternRules` policy (`allow: ["shell:git status*", "shell:uv run pytest*"]`,
  `deny: [...]`), placed before `ask`. Introduce `~/.jimbo/config.toml` + `.jimbo/config.toml`
  (pydantic-settings TOML source) so rules don't live in env vars.
- Sandbox (macOS `sandbox-exec` profile limiting writes to workspace) as an optional follow-up.

### Phase 3 — Paste: multiline, images, files — done
- `send()` accepts `str | list[UserContent]` (pydantic-ai `BinaryContent`/`ImageUrl`); event
  `user_message` gains `attachments` metadata (not bytes) for logs.
- TUI: confirm prompt_toolkit bracketed paste keeps newlines without submitting; collapse pastes over N
  lines into `[pasted #1: 240 lines]` placeholders expanded on submit. `@path` completion that inlines
  file contents (via `resolve_in_workspace`). Image paste: Ctrl+V binding reads clipboard image
  (macOS `osascript`/`pngpaste`) → attachment chip `[image #1]`.
- Web: `paste`/drop handlers on the textarea; images sent as base64 in the `send` message
  (size-capped), shown as thumbnails.
- Guard: warn if the model isn't vision-capable (setting flag per provider/model).

### Phase 4 — Todos — done
- `tools/todos.py`: `todo_write(items: list[{content, status: pending|in_progress|done}])` — replaces the
  whole list (simplest for small local models), read-only-approved. State lives on `Deps.todos`.
- Emit `todos` event; TUI renders a compact checklist after each update, web a sidebar panel;
  `/todos` prints it. Prompt nudges using it for 3+ step tasks.

### Phase 5 — Planning mode
- `PlanMode` approval policy at the head of the chain: allows read-only, denies everything else with
  "plan mode: present a plan instead". Toggle with `/plan` (or Shift+Tab in TUI).
- `exit_plan(plan: str)` tool → shown via `request_approval` (reuses existing dialog in all three
  UIs); approving removes `PlanMode` and seeds todos from the plan. Instructions gain a plan-mode section
  (pydantic-ai dynamic `@agent.instructions` reading `deps.mode`).

### Phase 6 — Project instructions and skills — done
- Dynamic instructions: load `AGENTS.md` / `JIMBO.md` from workspace root (+ `~/.jimbo/AGENTS.md`).
- Skills: discover `*/SKILL.md` in `~/.jimbo/skills` and `.jimbo/skills`; parse frontmatter
  (`name`, `description`); list name+description in instructions; `load_skill(name)` tool returns
  the body (read-only, works outside workspace). `/skills` lists them; `/<skill>` invokes directly
  through the command registry.

### Phase 6b — Project index
A compact, persistent per-file summary store so the agent can answer questions about files and skim the
codebase when planning, without reading whole files.
- **Layout:** `.jimbo/index/<relpath>.json` mirrors the workspace tree (O(1) lookup by path, atomic per-file
  writes). `.jimbo/index/.gitignore` containing `*` keeps it a local, git-ignored cache.
- **Entry:** `{v, path, sha256, size, mtime, lang, model, purpose, chunks:[{id, name, kind, start, end, sha, summary}]}`
  — one "what is this for" sentence plus a 1–2 sentence summary per chunk.
- **Hybrid summarizing:** deterministic chunking (Python `ast`, markdown headings, ~80-line windows otherwise) so line
  ranges are exact; a tool-less pydantic-ai call per file writes the prose. Oversize files fall back to map-reduce.
- **Separate model:** `Settings.index_model` (`"<provider>:<name>"`, default = follow the main model), also
  `/index model [<provider>:<name>|default]`. User-level config only (add to `UNTRUSTED_KEYS`). Each entry records the
  model that wrote it.
- **Freshness:** lazy — `size+mtime`, then sha256, checked on lookup; `write_file`/`edit_file` mark paths dirty and a
  background flush at run end refreshes them. No file watcher.
- **Module/tools:** `core/index.py` `ProjectIndex` (reuses `list_files` and `resolve_in_workspace`); read-only tools
  `index_lookup(path)` and `index_overview(path)` (the latter never calls a model); `/index build|status|clear|model`.
- **Later:** per-chunk re-summarizing on edit, tree-sitter chunking, embeddings/semantic search, injecting a
  top-level overview via Phase 6 dynamic instructions.
- **Flag:** with a remote provider, indexing sends file contents to it; `/index build` should say which model it uses.

### Phase 7 — Compaction, then auto-compaction
- `core/compaction.py`: `compact(history) -> history` — run the model with no tools on a summarize
  prompt (goal, decisions, files touched, open todos), return `[summary as UserPromptPart, ack]` plus
  the last K messages kept verbatim; never split a tool-call/tool-return pair.
  Cheap first stage: drop/truncate old large `ToolReturnPart` contents.
- `/compact [focus]` command; `compaction` event.
- Auto: after each turn, if context ratio (Phase 0.3) > `Settings.compact_threshold` (0.8), compact
  before the next send. Mid-run (long tool loops) via a pydantic-ai `history_processors` hook.

### Phase 8 — Steering
- While a run is in flight, frontends keep accepting input (TUI: prompt stays active below Live output
  — the hardest UI work in the plan; web already does this). Input goes to `session.steer(text)` →
  queue.
- A `history_processors` callable on the agent drains the queue and appends a `UserPromptPart`
  before each model request, so the model sees it at the next step without restarting the run.
  Emit `user_message(steer=True)`. Esc still cancels (Phase 0.1); queued steers left at run end
  become the next turn.

### Phase 9 — Persistence and resume
- Persist `ModelMessage`s (`ModelMessagesTypeAdapter`) alongside the event JSONL after each turn
  (and after compaction). `jimbo --resume [id]` / `/resume` reloads history and replays events to the UI.

### Phase 10 — Later
Subagents (`task` tool running a child `AgentSession` with read-only tools), MCP client
(pydantic-ai `MCPServer` toolsets through `authorize`), `web_fetch` tool, hooks, protocol docs for
events/WebSocket messages, LLM-guard approval policy.

## Critical files
- `src/jimbo/core/session.py` (cancel, steer, compaction, multimodal send)
- `src/jimbo/core/runner.py`, new `core/commands.py`, `core/compaction.py`
- `src/jimbo/core/events.py` (new kinds: `command_output`, `context`, `todos`, `compaction`)
- `src/jimbo/core/approval.py` (`PatternRules`, `PlanMode`)
- `src/jimbo/agent.py` (dynamic instructions, history processors), `src/jimbo/wiring.py`
- new `src/jimbo/tools/{shell,search,todos,skills}.py`
- `src/jimbo/frontends/{tui,web,cli_plain}.py`, `web_static/index.html`
- `src/jimbo/config.py` (context_window, compact_threshold, TOML config)

## Verification (per phase)
- Unit tests in the existing `tests/test_core.py` style: `ScriptedFrontend` + `FunctionModel` scripted
  tool calls (e.g. a model that calls `todo_write`, a run cancelled mid-stream, steer message visible
  in the next `ModelRequest`, compaction preserving tool-call pairs, `PlanMode` denying `write_file`).
- `tests/test_tui.py` / `tests/test_web.py` for rendering of new events and new WS messages.
- `uv run pytest` green after each phase; manual smoke test with `uv run jimbo --ui tui` and
  `--ui web` against a local ollama model for the interactive bits (cancel, paste, steering).

## Suggested first slice
Phase 0 + Phase 1 together (one PR): the task-based runner, command registry, context event and the
first six commands. It's small, touches every frontend once, and everything after it plugs in.
