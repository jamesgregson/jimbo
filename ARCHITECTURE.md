# Architecture

Jimbo is a small coding agent built on [Pydantic AI](https://ai.pydantic.dev). The design goal is a
**headless core** that knows nothing about any UI: it takes user input, runs the model and tools, and
emits a stream of JSON-serializable `Event`s. Frontends, loggers and tests are all just consumers of
that stream. This document explains how the pieces fit together; see the [README](README.md) for usage
and the [reference](docs/reference.md) for configuration.

## Layout

```
src/jimbo/
  main.py            CLI entry point: parse args, build frontend + session, run
  wiring.py          composition root: Settings + Frontend -> AgentSession
  agent.py           system prompt, tool list, builds the Pydantic AI Agent
  commands.py        built-in slash commands (/model, /approve, /clear, ...) and skill commands
  config.py          Settings (env, config.toml, CLI), trust rules for workspace config
  core/              headless, UI-free logic
    session.py         AgentSession: the agent loop driver; emits events
    events.py          Event model and the EventSink interface
    interaction.py     Frontend protocol (what a UI must implement)
    runner.py          pumps user input into the session; Ctrl+C -> cancel
    approval.py        approval policies and the ApprovalGate
    deps.py            Deps: per-session state handed to every tool; authorize()
    commands.py        CommandRegistry, markdown commands, frontmatter parsing
    skills.py          AGENTS.md loading and skill discovery
    agents.py          agent files, built-in agents, ForwardSink
    compaction.py      history summarization (/compact and auto-compaction)
    attachments.py     @path mentions and pasted images
    jsonl.py           JsonlSink: session log
    models.py          ModelRouter: which model runs each task
    llm_tasks.py       run_task: background model calls (None on failure/timeout)
    assess.py          shell command summary and risk level for approval prompts
    stats.py           token/context accounting helpers
    store.py           SessionStore: history + todos saved per turn (<id>.messages.json) for --resume
    checkpoints.py     Checkpoints: files as each turn found them, for /diff and /undo
  tools/             what the model can call (all go through Deps.authorize)
    fs.py paths.py     read_file, list_dir, write_file, edit_file; workspace path confinement
    search.py          glob, grep
    shell.py           shell (with timeout, output capping, process-group kill)
    sandbox.py         macOS sandbox-exec wrapper for shell
    todos.py skills.py todo_write, load_skill
    agents.py          run_agent
    plan.py            exit_plan (plan mode only)
  providers/         model selection: "<provider>:<name>" -> Pydantic AI Model; startup checks (preflight.py)
  frontends/         plain (stdin/stdout), tui (rich + prompt_toolkit), web (aiohttp + browser page), json
  bundled/           agents, skills and commands shipped with jimbo, laid out like .jimbo/
```

The dependency direction is strict: `core` imports nothing from `frontends`; frontends depend only on
the `Frontend` protocol and `Event`. Things that must rebuild agents or policies (the built-in slash
commands) live outside `core` for the same reason.

## Wiring

[wiring.py](src/jimbo/wiring.py) `build_session()` is the only place everything is assembled:

1. Build the approval policy instances and pick the chain from `settings.approval`.
2. Build the `Sandbox` (if enabled/available) and `Deps` (workspace, gate, sandbox, timeouts).
3. Load project instructions and skills from disk into `Deps`.
4. Build the Pydantic AI `Agent` and wrap it in an `AgentSession` with the sinks: the frontend (through
   `FrontendSink`) plus an optional `JsonlSink`.
5. Register slash commands: built-ins, then markdown commands, then one per skill, then bundled commands.
   Earlier registrations win, so user files can never override a built-in, and always override a bundled
   command.

Tests use the same function with a fake `model=`, so they exercise the real wiring.

## The agent loop

[core/session.py](src/jimbo/core/session.py) `AgentSession` owns the conversation `history` and runs one
*turn* per `send()`. A turn is Pydantic AI's `agent.iter()` graph, which alternates between two node types
until the model produces a final answer:

```
user input
   │  expand @path mentions, drop images if vision=false          emit user_message
   ▼
┌─────────────────────────────────────────────────────────────┐
│ ModelRequestNode   stream model output                       │
│                    └─ text chunks ─────────────────────────► assistant_text_delta
│                    └─ reasoning chunks ────────────────────► assistant_thinking_delta
│        │                                                     │
│        ▼                                                     │
│ CallToolsNode      record context size                       ► context
│                    for each tool call the model made:       │
│                      └─ announce ─────────────────────────► tool_call
│                      └─ tool runs (authorize -> execute)     │
│                      └─ result / retry message ───────────► tool_result
│        │  tool results go back to the model                  │
│        └──────────────► loop to ModelRequestNode ────────────┘
└─────────────────────────────────────────────────────────────┘
   ▼  final text                                  assistant_text, usage, run_finished
```

Key properties:

- **Bounded.** `request_limit` (default 50) caps model requests per turn via `UsageLimits`. For the last 3
  requests, `project_context` adds a notice telling the model to stop starting new work, leave the workspace
  in a sensible state and end with a summary. On the last request it is told to reply without tool calls.
- **Cancellable.** `send()` runs the turn in its own `asyncio.Task` so `cancel()` (Ctrl+C, or the web UI's
  Stop button) cancels the run without cancelling the caller. In the runner's SIGINT handler a second
  Ctrl+C within 2 seconds of one that cancelled a run escalates to quitting. On cancel, the partial turn is kept in
  history; `repair_messages` closes out tool calls that never got a result. Running shell commands are
  killed on the way out.
- **Fail soft.** Any exception becomes an `error` event followed by `run_finished(ok=False)`; the session
  stays usable and, as with a cancel, the partial turn is kept, because its file changes really happened.
- **Tool errors are model feedback.** Tools raise `ModelRetry` for bad arguments, path escapes and
  denials. The model sees the message and can adjust, up to `tool_retries` times. The system prompt tells
  it not to repeat a denied call.
- **Context for the model** is the static system prompt plus a dynamic `project_context` function that
  reads `Deps` on every request: `AGENTS.md`/`JIMBO.md` contents and the skill index.
- **Todos.** `todo_write` replaces the task list held in `Deps`; the session emits a `todos` event when it
  changes.

### Compaction

[core/compaction.py](src/jimbo/core/compaction.py) `compact()` summarizes old history with a tool-less
model call and returns `[summary, ack, *recent tail]`. The tail starts at a request that answers no tool
call, so call/return pairs are never split; old tool results are truncated in the transcript the
summarizer sees. `/compact [focus]` runs it on demand. When the context fill passes `compact_threshold`
(default 0.8 of the context window) it also runs automatically: before a turn in `send()`, and between
model requests in a long tool loop through a `ProcessHistory` capability (which never drops the pending
request). Failures are reported as `error` events and the run continues uncompacted.

### Input handling

[core/runner.py](src/jimbo/core/runner.py) `run_frontend()` is the interactive loop: read input from the
frontend, and for each message either dispatch it as a slash command or call `session.send()`. While a
run is in flight it installs a SIGINT handler that cancels the run instead of quitting. One-shot mode
(`jimbo "prompt"`) skips the runner and calls `send()` once; server-style frontends (web) stay up after the
first prompt.

**Steering.** A frontend with `supports_steering = True` (tui, web; not plain, whose approval prompts share
stdin) is polled for input during a run. The runner passes it to `session.steer()`, and the
`midrun_history` processor in [agent.py](src/jimbo/agent.py) appends queued steers to the next model request
as a `UserPromptPart` (emitting `user_message` with `steer=True`). Steers the run never delivered become the
next turn; a cancelled or failed run discards them with a notice. Slash commands are refused mid-run.
Denying an approval can carry a reason (`ApprovalDecision.note`), which the model receives as
`User denied this action: <note>`. In the TUI the approval and reason questions are answered through the
same prompt that takes steers.

Slash commands (`/name args`) are matched by `CommandRegistry.dispatch` *before* the model sees the text.
Text like `/Users/x/y` is not a command, so pasted paths go to the model. Custom markdown commands and
skill commands expand into a normal prompt and run through `session.send()`, so the model can use tools
as usual.

## Events

[core/events.py](src/jimbo/core/events.py) defines the single vocabulary shared by every consumer:

| Kind | Meaning |
|------|---------|
| `user_message` | the user's input, with attachment metadata |
| `assistant_text_delta` / `assistant_text` | streamed chunks / final answer |
| `assistant_thinking_delta` | streamed reasoning, when the model returns it; not replayed on resume |
| `tool_call` / `tool_result` | a tool invocation and its outcome |
| `approval_requested` / `approval_resolved` | a human was asked / any policy decided |
| `usage`, `context` | token counts; context-window fill |
| `todos` | the agent's task list changed |
| `compaction` | older history was replaced by a summary |
| `command_output`, `commands` | slash command results; the command list for completion |
| `error`, `run_finished` | failure; end of turn (`ok`, `cancelled`) |
| `status` | model, workspace, plan/auto/yolo mode and sandbox: sent at startup and when they change |
| `busy` | `label` and `on`: a slow command outside a run (`/compact`) started or finished; sent by `session.busy()` |

Events carry a sequence number, timestamp and session id, and must stay JSON-serializable. That is what
lets the same stream drive a terminal renderer, a websocket to the browser, and the JSONL session log
(`~/.jimbo/sessions/<id>.jsonl`, viewable with [tools/trace_viewer.html](tools/trace_viewer.html)).

## Frontends

A frontend implements three methods of the `Frontend` protocol
([core/interaction.py](src/jimbo/core/interaction.py)):

- `next_user_input()` returns the next message (text, or a `UserMessage` with images), or `None` to quit.
- `render(event)` displays an event; it must not block the run.
- `request_approval(req)` asks the human to allow or deny a tool call.

Four are included, chosen by `--ui` and created lazily by
[frontends/registry.py](src/jimbo/frontends/registry.py). `ui = "auto"` means `tui` for an interactive session
in a terminal and `plain` otherwise:

- **plain**: line-oriented stdin/stdout.
- **tui**: inline rich UI with streaming markdown, `@path` completion, paste collapsing, clipboard images.
- **json**: one JSON event per line on stdout, for scripts. Calls that need a human are denied.
- **web**: a local aiohttp server and single-page UI; approvals appear as a dialog with the diff. The header
  and the dialog each have an approval mode menu, which sends `approval_mode` (see The approval chain). The URL
  carries a one-time token because the page can approve file writes, so keep it on localhost.

To add one, implement the protocol and register a factory in `FRONTENDS`.

## Tools and the permission model

Every tool follows the same shape: validate arguments, call `ctx.deps.authorize(...)`, then act.
`Deps.authorize()` ([core/deps.py](src/jimbo/core/deps.py)) is the **single choke point**: it builds a
`ToolCallInfo` (tool name, args, a one-line summary, an optional diff/command `detail`, and a `read_only`
flag) and asks the `ApprovalGate`. Anything other than an `allow` verdict raises `ToolDenied`, which tools
convert to a `ModelRetry` so the model learns why.

Write tools compute their diff *before* asking, so the human approves the exact change that will be made.
`edit_file` also validates that `old` matches exactly once before prompting.

### The approval chain

[core/approval.py](src/jimbo/core/approval.py) separates **policy** (does this need a human?) from **UI**
(how is the human asked?). `ApprovalGate` runs an ordered list of policies; each returns `allow`, `deny` or
`ask`, and the **first non-`ask` verdict wins**. If no policy decides, the call is denied (fail closed).
The default chain is:

| Policy | Behavior |
|--------|----------|
| `plan` (`PlanMode`) | always first; in plan mode denies non-read-only calls and asks the user to approve `exit_plan` |
| `rules` (`PatternRules`) | `approval_deny` first, then `approval_allow`; a match decides, otherwise `ask` |
| `readonly` (`ReadOnlyAllow`) | allow calls flagged `read_only` (read, list, glob, grep, todos, load_skill, run_agent) |
| `remembered` (`RememberedDecisions`) | allow what the user chose "always" for: one exact shell command, or a whole other tool |
| `ask` (`AskUser`) | terminal policy: ask the frontend; honors "remember for session" |

`--auto` (or `/approve auto`) inserts `auto` (`AutoApprove`) before `ask`. It allows `write_file` and
`edit_file` (which can't leave the workspace), and shell calls whose assessment came from the model and is
`low` or `medium`. It returns `ask` for everything else, including shell calls with no model answer. It runs the assess hook itself, so the prompt for a call it passes on shows the
same assessment. It is built without the hook, and then allows nothing, when `jimbo-shell-assess` mode is `off`.

`--yolo` (or `/approve yolo`) swaps the chain for `rules` then `yolo`, so **deny rules still apply** when
everything else is auto-approved. `/approve default` restores the normal chain.

The named chains live in `ApprovalModes`, which `/approve` and frontend controls share. A frontend with its own
control defines `bind_approval_mode`; `run_frontend` hands it `set_approval_mode`, which, unlike a slash command,
works mid-run and while a prompt is open. It replaces `gate.policies`, so a call already being decided keeps the
old chain and later calls use the new one. A frontend with an optional `confirm(message) -> bool` (the TUI) is
asked before a risky mode is turned on: `run_frontend` sets it as `session.confirm`, `/approve auto|yolo` uses it,
and a session started in a risky mode is asked once and falls back to the default chain on "no". Because policies are plain
objects behind a protocol, a smarter one (for example an LLM-based guard) can be inserted before `ask`.

Details worth knowing:

- Rules look like `tool` or `tool:<glob>`, matched against the call's first argument (command or path).
  Paths are normalized first, and deny rules also match the file name. `secret_files` become deny rules on
  `read_file`, and `grep` skips those files.
- Frontends read an answer with `parse_answer`: only exact answers approve, anything else denies and is passed
  to the model as the reason.
- **Shell allow rules never match compound commands.** Anything with pipes, `;`, `&&`, redirects,
  backticks or `$(...)` is not auto-approved, so `shell:git status*` cannot approve `git status; rm -rf ~`.
  Deny rules are checked against each segment of a compound command. They are best effort, not a
  boundary.
- `AskUser` holds a lock so parallel tool calls never show two prompts at once.
- Every decision, whoever made it, goes through the gate's observer and is emitted as
  `approval_resolved` with `decided_by`, so logs show why a call ran. Approval events carry the `call_id` of
  their `tool_call`, because parallel calls can be decided in any order.

### Containment layers

Approvals are about intent; separate mechanisms limit what a call can physically do:

1. **Path confinement** ([tools/paths.py](src/jimbo/tools/paths.py)): file tools resolve symlinks and
   reject paths outside the workspace (`PathEscape`). Read-only tools may additionally read the folders of
   loaded skills.
2. **Sandbox** ([tools/sandbox.py](src/jimbo/tools/sandbox.py)): on macOS, `shell` is wrapped in
   `sandbox-exec` with a profile that allows reads anywhere but writes only to the workspace, temp and
   cache dirs plus `sandbox_writable`, and optionally blocks outbound network. The approval prompt states
   whether the command is sandboxed. This is the real boundary; deny rules are not.
3. **Shell hygiene** ([tools/shell.py](src/jimbo/tools/shell.py)): stdin is empty, a timeout applies
   (model-adjustable up to `shell_timeout_max`), the command runs in its own process group so the whole
   tree can be killed on timeout or interrupt, and output keeps only its head and tail when too long.
4. **Config trust** ([config.py](src/jimbo/config.py)): a cloned repo's `.jimbo/config.toml` cannot set
   `providers`, `model_aliases`, `models`, `tasks`, `secret_files`, keys, URLs, `workspace`, `web_host`, `log_dir`, `user_dir`, `approval` / `approval_allow`, or `sandbox*`; those are read only from `~/.jimbo/config.toml`. Its
   `approval_deny` rules are added to the user's, never replacing them.

## Extensibility

- **Project instructions**: `~/.jimbo/AGENTS.md` plus the workspace's `AGENTS.md` / `JIMBO.md`, each
  capped, injected into the model's instructions on every request.
- **Skills**: folders containing `SKILL.md` (frontmatter `name`, `description`), discovered from
  `.jimbo`, `.claude` and `.agents` skill directories at user and workspace level (later wins). Only the
  name/description index goes in the prompt; the model calls `load_skill(name)` for the full body, and the
  tool is hidden when no skills exist. Each skill is also a `/<name>` slash command.
- **Markdown commands**: `<dir>/<name>.md` files in `~/.jimbo/commands` and `<workspace>/.jimbo/commands`
  become `/<name>`, with `$ARGUMENTS` substitution. With `agent:` in the frontmatter they run in that agent.
- **Agents**: `<dir>/<name>.md` files in `~/.jimbo/agents` and `<workspace>/.jimbo/agents`, plus the built-in
  `explore` and `general` ([core/agents.py](src/jimbo/core/agents.py)). See [Agents](#agents).
- **Bundled files**: `src/jimbo/bundled/` holds agents, skills and commands in the same formats. Its directories
  are lowest precedence, and `bundled = false` skips them. `tests/test_bundled.py` checks the real files.
- **Providers**: `"<provider>:<model>"` resolves through
  [providers/registry.py](src/jimbo/providers/registry.py) to a per-provider `ProviderConfig` (endpoint plus
  one credential source, resolved lazily in [providers/credentials.py](src/jimbo/providers/credentials.py)).
  All current adapters speak the OpenAI-compatible API; `/model` swaps the agent mid-session without losing
  history.
- **Model routing**: [core/models.py](src/jimbo/core/models.py) `ModelRouter` maps a task name (`main`,
  `jimbo-shell-assess`, `jimbo-compaction`, ...) to a model: skill scope (aliases only) > `/models set` >
  `[models]` > the required `main` alias. Agents use `for_agent`: `/models set` > `[models]` > the alias in their
  file > `main`. It lives on `Deps.models`; background calls go through
  [core/llm_tasks.py](src/jimbo/core/llm_tasks.py) `run_task`, which returns None on failure or timeout.
- **Shell assessment**: [core/assess.py](src/jimbo/core/assess.py). `AskUser` calls an assess hook just
  before prompting, so only calls that need a human cost a model request (mode `always` also covers allowed
  calls via the gate observer). The model's risk is combined with a deterministic floor by `max`; the result
  rides on `ApprovalRequest.assessment` and the `approval_requested` / `approval_resolved` events.
- **Attachments**: `@path` mentions are inlined (capped) for the model while the logged `user_message`
  keeps the original text; pasted images are validated for type and size and dropped with a warning when
  `vision = false`.

## Agents

An agent run is a child `AgentSession` with `nested=True`, so compaction, cancellation and usage tracking are the
same code as the main loop. `core` can't import `build_agent`, so [wiring.py](src/jimbo/wiring.py) sets
`Deps.spawn_agent`, which is the single way to start one. `run_agent` calls it from a tool, with `sequential=True` so
agents run one at a time. `AgentSession.delegate()` calls it directly for `@name` and commands with `agent:`.

- **Deps.** The child gets `replace(parent.deps, ...)` with its own router, empty todos and `agents={}`. Empty
  `agents` is what stops agents starting agents. The gate, sandbox and checkpoints stay shared, so approvals and
  plan mode apply unchanged. `Deps.agent` adds the agent's name to approval summaries.
- **Checkpoints.** Nested sessions skip `checkpoints.begin()`, so an agent's edits belong to the turn that started
  it, and one `/undo` covers them. `delegate()` calls `begin()` itself because no parent turn is running.
- **Events.** `ForwardSink` passes the child's `tool_call`, `tool_result`, `compaction` and `error` events to the
  parent, tagged `agent={name, model}`, and drops the rest. Frontends only add a prefix.
- **Models.** Each model an agent uses is checked once with `inspect_model`, and its context window applies to
  the child. An agent on the main model uses the main session's window.
- **Limits.** Child usage is added to the parent's, so `request_limit` stays a per-turn total. `run_agent` keeps 5
  requests for the parent. Failures return text instead of raising `ModelRetry`, because a retry could repeat file
  changes that already happened.

## Testing

`uv run pytest`. Tests build sessions through `build_session()` with a scripted model from
`tests/helpers.py` and assert on emitted events, so they cover the loop, approvals, commands and tools
without any network or UI.
