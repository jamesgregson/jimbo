# Architecture

Jimbo is a coding agent built on [Pydantic AI](https://ai.pydantic.dev). See the [README](README.md) for usage
and the [reference](docs/reference.md) for configuration.

## What jimbo does

- Runs a model in a loop with tools that read, search and edit files in a workspace, run shell commands, and
  query language servers.
- Asks a human before calls that change things, according to a configurable chain of approval policies. On
  macOS, shell commands run in a sandbox.
- Runs in a terminal (`tui`, `plain`), in a browser (`web`), or as a JSON event stream for scripts (`json`).
- Loads project instructions (`AGENTS.md`), skills, slash commands and agents from files.
- Delegates work to agents: child sessions with their own instructions and model.
- Saves each session: history for `--resume`, file checkpoints for `/diff` and `/undo`, and a JSONL event log
  that `/trace` shows in a browser.
- Summarizes old history when the context window fills (compaction).
- Uses different models for different tasks (main conversation, shell risk assessment, compaction, agents).

## Overview

```
                ┌──────────────────────── Frontend ─────────────────────────┐
                │  tui · plain · web · json                                 │
                │  next_user_input()     render(event)     request_approval │
                └──────┬────────────────────────▲───────────────────▲───────┘
                       │ input                  │ events            │ ask
                       ▼                        │                   │
   Runner ── "/name" ──► CommandRegistry        │                   │
       │ send(), steer(), cancel()              │                   │
       ▼                                        │                   │
   AgentSession ──────── emits Events ──────────┤                   │
       │   history, compaction                  └──► JsonlSink (session log)
       ▼
   Pydantic AI Agent ◄──────► Model (providers/, chosen by ModelRouter)
       │ tool calls
       ▼
   Tools ── Deps.authorize() ──► ApprovalGate (policy chain) ───────┘
       ├── files: path confinement, Checkpoints
       ├── shell: Sandbox
       ├── lsp_*: LspManager ──► language server processes
       └── run_agent: child AgentSession (same Deps, gate and sandbox)
```

## Subsystems

| Subsystem | Code | Role |
|---|---|---|
| Entry and wiring | [main.py](src/jimbo/main.py), [wiring.py](src/jimbo/wiring.py) | Parse arguments, load `Settings`, build the frontend and the `AgentSession` |
| Session | [core/session.py](src/jimbo/core/session.py) | Holds the history and runs one turn per `send()`; emits events |
| Prompts | [agent.py](src/jimbo/agent.py), [core/session.py](src/jimbo/core/session.py) | Build the instructions for each request and the user messages the model sees |
| Runner | [core/runner.py](src/jimbo/core/runner.py) | Reads input from the frontend; dispatches slash commands or calls `send()`; handles Ctrl+C and steering |
| Events | [core/events.py](src/jimbo/core/events.py) | The JSON-serializable messages the session emits to every consumer |
| Frontends | [frontends/](src/jimbo/frontends/) | Show events, collect input, answer approval prompts |
| Tools | [tools/](src/jimbo/tools/) | Functions the model can call; each one calls `Deps.authorize()` before acting |
| Approvals | [core/approval.py](src/jimbo/core/approval.py) | `ApprovalGate` runs an ordered list of policies to allow, deny or ask |
| Containment | [tools/paths.py](src/jimbo/tools/paths.py), [tools/sandbox.py](src/jimbo/tools/sandbox.py), [config.py](src/jimbo/config.py) | Workspace path confinement, macOS sandbox, trust rules for workspace config |
| Models | [providers/](src/jimbo/providers/), [core/models.py](src/jimbo/core/models.py) | Turn `"<provider>:<name>"` into a model; pick the model for each task |
| Extensions | [core/skills.py](src/jimbo/core/skills.py), [core/commands.py](src/jimbo/core/commands.py), [core/agents.py](src/jimbo/core/agents.py), [bundled/](src/jimbo/bundled/) | Instructions, skills, markdown commands and agents read from disk |
| Language servers | [lsp/](src/jimbo/lsp/) | LSP client and server lifecycle |
| Persistence | [core/store.py](src/jimbo/core/store.py), [core/checkpoints.py](src/jimbo/core/checkpoints.py), [core/jsonl.py](src/jimbo/core/jsonl.py) | Saved history, file checkpoints, event log |

`core/` imports nothing from `frontends/`. Frontends depend only on the `Frontend` protocol and `Event`. Code
that rebuilds agents or policies (the built-in slash commands in [commands.py](src/jimbo/commands.py)) is
outside `core/` for the same reason.

## How a turn runs

1. The frontend returns a line of input. The runner sends `/name ...` to the `CommandRegistry`; anything else
   goes to `AgentSession.send()`.
2. The session expands `@path` mentions, emits `user_message`, and starts a Pydantic AI run with the history.
   Each model request gets instructions rebuilt from the system prompt and the session's current state
   (mode, project instructions, skill and agent indexes). See [Prompts](#prompts).
3. The model streams text and tool calls. Text becomes `assistant_text_delta` events.
4. Each tool call is emitted as `tool_call`. The tool validates its arguments and calls `Deps.authorize()`,
   which asks the `ApprovalGate`. The gate's policies allow, deny, or ask the frontend.
5. The tool runs, or raises `ModelRetry` with the reason it was denied. The result is emitted as `tool_result`
   and sent back to the model.
6. Steps 3 to 5 repeat until the model replies without tool calls or the request limit is reached.
7. The session emits `assistant_text`, `usage` and `run_finished`, and saves the history.

Every event goes to each sink: the frontend and the JSONL log.

## Layout

```
src/jimbo/
  main.py            CLI entry point: parse args, build frontend + session, run
  wiring.py          composition root: Settings + Frontend -> AgentSession
  agent.py           system prompt, tool list, builds the Pydantic AI Agent
  commands.py        built-in slash commands (/model, /providers, /approve, /clear, ...) and skill commands
  config.py          Settings (env, config.toml, CLI), trust rules for workspace config
  config_edit.py     ConfigControl: saves model and provider changes to ~/.jimbo/config.toml and applies them
  core/              UI-free logic
    session.py         AgentSession: runs turns; emits events
    events.py          Event model and the EventSink interface
    interaction.py     Frontend protocol (what a UI must implement)
    runner.py          pumps user input into the session; Ctrl+C -> cancel
    approval.py        approval policies and the ApprovalGate
    deps.py            Deps: per-session state handed to every tool; authorize()
    commands.py        CommandRegistry, markdown commands, frontmatter parsing
    skills.py          AGENTS.md loading and skill discovery
    agents.py          agent files, built-in agents, ForwardSink
    compaction.py      history summarization (/compact and auto-compaction)
    attachments.py     @path mentions (text inlined, images attached) and pasted images
    jsonl.py           JsonlSink: session log
    trace.py           the trace viewer page (trace_viewer.html) with a session's log embedded, for /trace
    models.py          ModelRouter: which model runs each task
    llm_tasks.py       run_task: background model calls (None on failure/timeout)
    assess.py          shell command summary and risk level for approval prompts
    questions.py       ask_user's questions and answers; reading a typed reply
    stats.py           token/context accounting helpers
    cost.py            estimated cost in USD of model requests
    store.py           SessionStore: history + todos saved per turn (<id>.messages.json) for --resume
    checkpoints.py     Checkpoints: files as each turn found them, for /diff and /undo
  tools/             what the model can call (all go through Deps.authorize)
    fs.py paths.py     read_file (images returned as images), list_dir, write_file, edit_file, multi_edit, delete_file,
                       move_file; workspace path confinement
    search.py          glob, grep
    shell.py           shell (with timeout, output capping, process-group kill); background processes, shell_output,
                       shell_kill
    web.py             web_fetch (Pydantic AI's local fetcher, behind an approval)
    show.py            show_image: emits an `image` event (path, not bytes); only frontends with shows_images offer it
    sandbox.py         the Sandbox interface, spawn/kill, and the macOS sandbox-exec sandbox
    container_sandbox.py  the Docker or Podman sandbox: one container per session, port forwards, images
    todos.py skills.py todo_write, load_skill, reload_skills (main session only)
    agents.py          run_agent
    ask.py             ask_user (only when the frontend can ask questions)
    plan.py            exit_plan (plan mode only)
    compact.py         compact: the model asks for /compact when the user tells it to
    memory.py          remember: saves a note to ~/.jimbo/memory.md or .jimbo/memory.md (main session only)
    lsp.py             lsp_definition, lsp_references, lsp_symbols, lsp_diagnostics, lsp_rename; errors after edits
  lsp/               language servers: client.py (JSON-RPC over stdio), servers.py (which server for which files),
                     manager.py (LspManager: start on first use, restart, keep open files in sync)
  providers/         "<provider>:<name>" -> Pydantic AI Model; credentials, rate limits, startup checks (preflight.py)
  frontends/         plain (stdin/stdout), tui (rich + prompt_toolkit), web (aiohttp + browser page), json
  bundled/           agents, skills and commands shipped with jimbo, laid out like .jimbo/
```

# Deeper dives

## Wiring

[wiring.py](src/jimbo/wiring.py) `build_session()` assembles everything:

1. Build the approval policy instances and pick the chain from `settings.approval`.
2. Build the `Sandbox` (if enabled and available), `Deps` (workspace, gate, sandbox, timeouts), and the
   `LspManager` when `lsp` is on.
3. Load project instructions and skills from disk into `Deps`.
4. Build the Pydantic AI `Agent` and wrap it in an `AgentSession` with the sinks: the frontend (through
   `FrontendSink`) and an optional `JsonlSink`.
5. Register slash commands: built-ins, then markdown commands, then one per skill, then bundled commands.
   Earlier registrations win, so user files cannot override a built-in, and do override a bundled command.

Tests call the same function with a fake `model=`.

## The agent loop

[core/session.py](src/jimbo/core/session.py) `AgentSession` owns the conversation `history` and runs one
turn per `send()`. A turn is Pydantic AI's `agent.iter()` graph, which alternates between two node types
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

- **Request limit.** `request_limit` (default 50) caps model requests per turn via `UsageLimits`. For the last 3
  requests, the `WrapUp` capability adds a notice to the end of the request telling the model to stop starting
  new work, leave the workspace in a sensible state and end with a summary. The notice is not saved in the
  history. On the last request the tools stay but `tool_choice` is `"none"`, and the model is told to reply with
  text only. Any tool call in that reply, real or written as text, is removed; if no text is left, a note says
  the turn ended without a final message.
- **Cancel.** `send()` runs the turn in its own `asyncio.Task`, so `cancel()` (Ctrl+C, or the web UI's Stop
  button) cancels the run without cancelling the caller. A second Ctrl+C within 2 seconds of one that cancelled
  a run quits. On cancel, the partial turn is kept in history; `repair_messages` closes out tool calls that
  never got a result. Running shell commands are killed.
- **Errors.** Any exception becomes an `error` event followed by `run_finished(ok=False)`. The session stays
  usable and, as with a cancel, the partial turn is kept, because its file changes happened.
- **Tool errors.** Tools raise `ModelRetry` for bad arguments, path escapes and denials. The model sees the
  message, up to `tool_retries` times. The system prompt tells it not to repeat a denied call.
- **Instructions.** The model gets the system prompt plus `project_context`, which reads `Deps` on every
  request. See [Prompts](#prompts).
- **Todos.** `todo_write` replaces the task list held in `Deps`; the session emits a `todos` event when it
  changes.
- **Usage and cost.** After each run the session adds token counts and an estimated cost
  ([core/cost.py](src/jimbo/core/cost.py)) to the session and run totals and emits `usage`.

### Compaction

[core/compaction.py](src/jimbo/core/compaction.py) `compact()` summarizes old history with a tool-less
model call and returns `[summary, ack, *recent tail]`. The tail starts at a request that answers no tool
call, so call/return pairs are not split; old tool results are truncated in the transcript the summarizer
sees. `/compact [focus]` runs it on demand. When the context fill passes `compact_threshold` (default 0.8 of
the context window) it runs automatically: before a turn in `send()`, and between model requests through a
`ProcessHistory` capability (which never drops the pending request). Failures are reported as `error` events
and the run continues uncompacted. The `compact` tool sets `Deps.compact_requested`, and the same
`ProcessHistory` hook compacts before the next request, with the tool's `focus`.

### Input handling

[core/runner.py](src/jimbo/core/runner.py) `run_frontend()` reads input from the frontend, and for each message
either dispatches it as a slash command or calls `session.send()`. While a run is in flight it installs a SIGINT
handler that cancels the run instead of quitting. One-shot mode (`jimbo "prompt"`) skips the runner and calls
`send()` once; server-style frontends (web) stay up after the first prompt.

Slash commands (`/name args`) are matched by `CommandRegistry.dispatch` before the model sees the text. Text like
`/Users/x/y` is not a command, so pasted paths go to the model. Markdown commands and skill commands expand into
a prompt and run through `session.send()`.

**Steering.** A frontend with `supports_steering = True` (tui, web; not plain, whose approval prompts share
stdin) is polled for input during a run. The runner passes it to `session.steer()`, and the `midrun_history`
processor in [agent.py](src/jimbo/agent.py) appends queued steers to the next model request as a
`UserPromptPart` (emitting `user_message` with `steer=True`). Steers the run never delivered become the next
turn; a cancelled or failed run discards them with a notice. Slash commands are refused mid-run, except those
marked `midrun` (`/approve`) and those with a `steer` handler: a skill's command returns its prompt, which is
queued as a steer, and applies the skill's frontmatter at once.

Denying an approval can carry a reason (`ApprovalDecision.note`), which the model receives as
`User denied this action: <note>`. In the TUI the approval and reason questions are answered through the same
prompt that takes steers.

## Prompts

Each model request has two parts:

- **Instructions**: rebuilt for every request and not stored in the history. Pydantic AI sends them as the
  system prompt.
- **Messages**: the conversation history plus the new user message. This is what is saved and resumed.

```
instructions (per request)                      messages (saved)
┌───────────────────────────────────┐           ┌──────────────────────────────────────┐
│ SYSTEM_PROMPT                     │           │ history (may start with a compaction │
│ AGENT_PROMPT        (agents only) │           │   summary)                           │
│ project_context(ctx):             │           │ user message: text + @path files     │
│   # Plan mode                     │           │   + images                           │
│   # user/project instructions     │           │ steers typed during the run          │
│   # user/project memory           │           │ wrap-up notice (last 3 requests,     │
│   # Skill <name>    (agents only) │           │   not saved)                         │
│   # Skills  (index)               │           └──────────────────────────────────────┘
│   # Agents  (index, main only)    │           tools: each tool's docstring
└───────────────────────────────────┘
```

### Instructions

`build_agent` in [agent.py](src/jimbo/agent.py) gives the Pydantic AI `Agent` a list of instructions, joined in
this order:

1. `SYSTEM_PROMPT`, with `{os}` set to `platform.system()`. It says what jimbo is, which tools to use for what,
   to read a file before editing it, to plan tasks of 3 or more steps with `todo_write`, to be concise, and not
   to retry a denied tool call.
2. `AGENT_PROMPT`, only in an agent run (see [Agent prompts](#agent-prompts)).
3. `project_context(ctx)`, called on every request. It reads the session's `Deps`, so changes to mode, skills
   or agents apply on the next request without rebuilding the agent.

`project_context` builds its text from these sections, each included only when it applies, joined by blank
lines:

| Section | When | Content |
|---|---|---|
| `# Plan mode` | `deps.mode == "plan"` | `PLAN_PROMPT` for the main agent: only read-only tools work, finish by calling `exit_plan(plan)`. `AGENT_PLAN_PROMPT` in an agent run: only read-only tools work, report what you find. |
| `# user instructions (...)`, `# project instructions (...)`, `# user memory (...)`, `# project memory (...)` | the files exist | `deps.project_instructions` (see below). |
| `# Skill <name>` | an agent run whose spec lists `skills:` | The full body of each listed skill, with its directory. |
| `# Skills` | skills exist and the agent may call `load_skill` | One line per skill (`- name: description`) and an instruction to call `load_skill(name)` when a description matches the task. |
| `# Agents` | agents with a description exist | An index of agents and an instruction to call `run_agent(agent, task)`. Empty in agent runs, because they get `agents={}`. |

When a language server was found, `project_context` also adds a short section naming the languages that have
one.

**Project instructions.** `load_project_instructions` in [core/skills.py](src/jimbo/core/skills.py) reads, in
order:

- `~/.jimbo/AGENTS.md`, `~/.jimbo/JIMBO.md`
- `<workspace>/AGENTS.md`, `<workspace>/JIMBO.md`
- `~/.jimbo/memory.md`, `<workspace>/.jimbo/memory.md`: notes saved with the `remember` tool

Each non-empty file becomes a section headed `# user instructions (<file>)` or `# project instructions (<file>)`,
or for the memory files `# user memory (...)` and `# project memory (...)`. Each file is cut at 20,000
characters, with `[truncated]` added. The text is loaded when the session is built, and again after `remember`
saves a note or `/undo` restores a memory file.

**Skills.** `load_skills` reads `<dir>/<name>/SKILL.md` from these directories, later ones overriding earlier
ones: bundled skills, `~/.claude/skills`, `~/.agents/skills`, `~/.jimbo/skills`, `<workspace>/.claude/skills`,
`<workspace>/.agents/skills`, `<workspace>/.jimbo/skills`. Only the name and the frontmatter `description` go
into the instructions. The body reaches the model in one of three ways:

- The model calls `load_skill(name)`. The tool result is `Skill directory: <path>` followed by the body. If
  another skill marked `interactive: false` is in use this turn, a note says to decide instead of asking the
  user.
- The user types `/<skill> [args]`. The user message is `Use the skill '<name>'. Its instructions:\n\n<body>`,
  followed by the args.
- An agent spec lists the skill under `skills:`. The body goes into that agent's instructions.

**Logging.** The session emits an `instructions` event with the full text the first time and whenever it
changes (`AgentSession._emit_instructions`). This is how the instructions appear in the session log and the
trace viewer.

### User messages

`AgentSession._compose` in [core/session.py](src/jimbo/core/session.py) turns the user's input into what the
model sees:

1. **Text.** The typed text, as is.
2. **`@path` mentions.** `expand_mentions` in [core/attachments.py](src/jimbo/core/attachments.py) appends each
   mentioned workspace file once, after a blank line, as `<file path="<path>">\n<contents>\n</file>`. Files are
   cut at 64,000 bytes with `[truncated]`. Mentions that are not readable files inside the workspace stay plain
   text.
3. **Images.** Pasted images are added as separate content parts after the text. If the model has
   `vision = false`, they are dropped and the user sees a warning.

The user is shown their original text; the model gets the expanded text.

- **Markdown commands** (`load_markdown_commands` in [core/commands.py](src/jimbo/core/commands.py)): the file
  body is the prompt. `$ARGUMENTS` in the body is replaced with the args; if the body has no `$ARGUMENTS`, the
  args are added after a blank line. With `agent:` in the frontmatter, the prompt goes to that agent instead of
  the main model.
- **Skill commands**: see Skills above.
- **Steers**: a message sent during a run is queued (`AgentSession.steer`). Before the next model request,
  `_inject_steers` composes it as above and appends it as an extra user part to the last request in the
  history. A steer still queued when the run ends is sent as the next turn.

### Text jimbo adds to the conversation

- **Wrap-up notice.** The `WrapUp` capability in [agent.py](src/jimbo/agent.py) watches the turn's request
  limit. When 3 or fewer requests are left, it appends `WRAP_UP_PROMPT` as a user part to the last message of
  the request. The notice says how many requests are left and asks the model to stop starting new work, leave
  the workspace in a sensible state, update the todo list and end with a status message. On the last request
  it adds `LAST_REQUEST_NOTE` and sets `tool_choice` to `"none"`. The notice is in the messages rather than the
  instructions because models in a long tool loop follow recent messages more than the system prompt. It is
  added to the request only and not saved in the history. If the last reply has no text, or only tool calls
  written as text, jimbo removes them and uses `NO_FINAL_MESSAGE` as the reply.
- **Compaction summary.** `compact` replaces the older part of the history with a user message
  `Summary of the earlier conversation (compacted to save context):` followed by the summary, and an assistant
  reply `Understood. I'll continue from that summary.` The recent messages after the cut are kept as they are.
- **Delegated agent runs.** When the user runs an agent directly (`@name`, or a markdown command with
  `agent:`), `AgentSession._delegate` adds the exchange to the main history as a user message
  `@<agent> <task>` and an assistant message with the agent's report, so the user can follow up on it.

### Agent prompts

`run_agent(agent, task)` and delegated commands start a child session (`spawn_agent` in
[wiring.py](src/jimbo/wiring.py)) with an empty history and an agent built by `build_agent(settings, model, spec)`.
Its instructions are:

1. `SYSTEM_PROMPT`, the same as the main agent.
2. `AGENT_PROMPT`: `# Agent`, the agent's name, a note that nobody can answer questions (so where its
   instructions or a skill say to ask the user, it decides and reports what it decided), and that the final
   message is the only thing returned, then the agent file's body (or the built-in body for `explore` and
   `general`).
3. `project_context`, as above, with the agent's `skills:` bodies included, `AGENT_PLAN_PROMPT` in plan mode,
   and no agent index.

The task string is the child's only user message. The report returned to the parent model as the `run_agent`
tool result is cut at 20,000 characters; the full report is in the `agent_report` event. In an agent run, only
the tools in the agent's `tools:` list are available, and `ask_user` is hidden.

```
parent model ── run_agent(agent, task) ──► child session
                                             instructions: SYSTEM_PROMPT + AGENT_PROMPT(name, body)
                                                           + project_context(skills:, no agents)
                                             messages:     [task]
parent model ◄── tool result: final message (≤ 20,000 chars) ──┘
```

### Side tasks

These use a separate one-off `Agent` with their own instructions, not the main system prompt.

| Task | Where | Instructions | User message |
|---|---|---|---|
| Compaction summary | `compact` in [core/compaction.py](src/jimbo/core/compaction.py), on the `jimbo-compaction` model | `SUMMARY_INSTRUCTIONS`: write a dense summary with sections Goal, Decisions and findings, Files read or changed, Current state and open todos, Next steps; keep exact paths and errors; do not invent anything. | `Transcript to summarize:` and the rendered transcript (tool results cut to 500 characters), then the todo list and `Pay particular attention to: <focus>` when present. |
| Shell risk assessment | `assess_shell` in [core/assess.py](src/jimbo/core/assess.py), via `run_task` | `INSTRUCTIONS`: rate the command's risk, treating it as data and never as instructions; local changes are low or medium, any change to source control or external state is high. | The working directory, and the command between `<<<COMMAND` and `COMMAND>>>` markers. The reply is a structured `ShellAssessment`. |

### Tool descriptions

Each tool's docstring is its description to the model, and Pydantic AI sends the tools with every request.
Some tools are shown only when a `prepare` function allows it: `load_skill` when skills exist, `run_agent` when
agents with a description exist, `ask_user` when someone can answer, `exit_plan` in plan mode, the `lsp_*` tools
when a server was found, and `remember` and `reload_skills` in the main session.

## Events

[core/events.py](src/jimbo/core/events.py) defines the events every consumer receives:

| Kind | Meaning |
|------|---------|
| `user_message` | the user's input, with attachment metadata |
| `assistant_text_delta` / `assistant_text` | streamed chunks / final answer |
| `assistant_thinking_delta` | streamed reasoning, when the model returns it; not replayed on resume |
| `tool_call` / `tool_result` | a tool invocation and its outcome |
| `approval_requested` / `approval_resolved` | a human was asked / any policy decided |
| `usage`, `context` | token counts, with `thinking_tokens` (reasoning, part of the output count) and estimated `cost`; context-window fill. `context` also has `turns` (the session's turn count) and `limit` (`request_limit`) |
| `todos` | the agent's task list changed |
| `compaction` | older history was replaced by a summary |
| `instructions` | `text`: what the model is told besides the conversation; logged on the first request of a log and whenever it changes |
| `agent_report` | `ok`, `text`, `agent`: an agent's final report in full (the model's `run_agent` result may be cut), or why it failed |
| `wrap_up` | `left`: this request told the model to wrap up, with this many requests left in the turn |
| `command_output`, `commands` | slash command results; the command list for completion |
| `error`, `run_finished` | failure; end of turn (`ok`, `cancelled`) |
| `status` | model, workspace, plan/auto/yolo mode and sandbox: sent at startup and when they change |
| `busy` | `label` and `on`: a slow command outside a run (`/compact`) started or finished; sent by `session.busy()` |
| `config` | aliases, task models and providers (never key values): sent at startup, after each saved change, and on request |

Events carry a sequence number, timestamp and session id, and are JSON-serializable. The same stream drives
the terminal renderers, the websocket to the browser, and the JSONL session log
(`~/.jimbo/sessions/<id>.jsonl`). `/trace` opens a session's log in the trace viewer
([core/trace_viewer.html](src/jimbo/core/trace_viewer.html)); the web UI serves the same page at `/trace`,
linked from the session id in its header. The viewer also opens any log dropped onto it.

## Frontends

A frontend implements three methods of the `Frontend` protocol
([core/interaction.py](src/jimbo/core/interaction.py)):

- `next_user_input()` returns the next message (text, or a `UserMessage` with images), or `None` to quit.
- `render(event)` displays an event; it must not block the run.
- `request_approval(req)` asks the human to allow or deny a tool call.

`--ui` picks one, created by [frontends/registry.py](src/jimbo/frontends/registry.py). `ui = "auto"` means
`tui` for an interactive session in a terminal and `plain` otherwise.

- **plain**: line-oriented stdin/stdout.
- **tui**: inline rich UI with streaming markdown, `@path` completion, paste collapsing, clipboard images.
- **json**: one JSON event per line on stdout. Calls that need a human are denied.
- **web**: a local aiohttp server and single-page UI; approvals appear as a dialog with the diff. The header
  and the dialog each have an approval mode menu, which sends `approval_mode`. The URL carries a one-time
  token because the page can approve file writes.

Optional methods: `confirm(message)` (yes/no), `ask_secret(message)` (a line that isn't echoed or kept in
history, for API keys), `ask_questions(questions)`, and `bind_cancel`, `bind_approval_mode`, `bind_config` and
`bind_model_list`, which `run_frontend` calls with functions the frontend's own controls can use.

To add a frontend, implement the protocol and register a factory in `FRONTENDS`.

**Questions.** The `ask_user` tool ([tools/ask.py](src/jimbo/tools/ask.py)) calls `Deps.ask_user`, which wiring
sets to the frontend's `ask_questions`. `Deps.can_ask` hides the tool when the frontend has no such method
(json), in agents, and for the rest of a turn in which a skill marked `interactive: false` was used (`use_skill`
sets `Deps.no_questions`; `send()` clears it). A frontend returns `Answers`: one answer or `None` per question,
and `feedback` if the user stopped to say something else. tui and plain ask one question at a time through
`ask_by_line` ([core/questions.py](src/jimbo/core/questions.py)): a line that isn't an answer ends the round as
feedback. web shows one dialog with a feedback box, and a `send` while questions are open is feedback too.
`format_answers` turns the result into text for the model. The questions and answers are in the `tool_call` and
`tool_result` events.

## Tools and approvals

Every tool validates its arguments, calls `ctx.deps.authorize(...)`, then acts. `Deps.authorize()`
([core/deps.py](src/jimbo/core/deps.py)) builds a `ToolCallInfo` (tool name, args, a one-line summary, an
optional diff or command `detail`, and a `read_only` flag) and asks the `ApprovalGate`. Any verdict other than
`allow` raises `ToolDenied`, which tools convert to a `ModelRetry`.

Write tools compute their diff before asking, so the human approves the exact change. `edit_file` also checks
that `old` matches exactly once before prompting.

### The approval chain

[core/approval.py](src/jimbo/core/approval.py) separates policy (does this need a human?) from UI (how is the
human asked?). `ApprovalGate` runs an ordered list of policies; each returns `allow`, `deny` or `ask`, and the
first non-`ask` verdict wins. If no policy decides, the call is denied. The default chain:

| Policy | Behavior |
|--------|----------|
| `plan` (`PlanMode`) | always first; in plan mode denies non-read-only calls except `lsp_start`, and asks the user to approve `exit_plan` |
| `rules` (`PatternRules`) | `approval_deny` first, then `approval_allow`; a match decides, otherwise `ask` |
| `readonly` (`ReadOnlyAllow`) | allows calls flagged `read_only` (read, list, glob, grep, todos, load_skill, reload_skills, run_agent, ask_user) |
| `remembered` (`RememberedDecisions`) | allows what the user chose "always" for: one exact shell command, one language server, or a whole other tool |
| `ask` (`AskUser`) | asks the frontend; honors "remember for session" |

`--auto` (or `/approve auto`) inserts `auto` (`AutoApprove`) before `ask`. It allows `write_file` and
`edit_file` (which can't leave the workspace), and shell calls whose model assessment is `low` or `medium`. It
returns `ask` for everything else, including shell calls with no model answer. It runs the assess hook itself,
so the prompt for a call it passes on shows the same assessment. When `jimbo-shell-assess` mode is `off` it is
built without the hook and allows nothing.

`--yolo` (or `/approve yolo`) replaces the chain with `rules` then `yolo`, so deny rules still apply.
`/approve default` restores the default chain.

The named chains live in `ApprovalModes`, shared by `/approve` and frontend controls. `run_frontend` hands a
frontend's `bind_approval_mode` the function `set_approval_mode`, which works mid-run and while a prompt is open.
It replaces `gate.policies`, so a call already being decided keeps the old chain. The tui and plain frontends use
it for `/approve <mode>` typed at an approval prompt: the mode switches and the same call is asked about again.
A frontend with `confirm(message)` (the TUI) is asked before `auto` or `yolo` is turned on; a session started in
one of those modes is asked once and falls back to the default chain on "no".

- Rules look like `tool` or `tool:<glob>`, matched against the call's first argument (command or path). Paths
  are normalized first, and deny rules also match the file name. `secret_files` become deny rules on
  `read_file`, and `grep` skips those files.
- Frontends read an answer with `parse_answer`: only exact answers approve; anything else denies and is passed
  to the model as the reason.
- Shell allow rules never match compound commands. A command with pipes, `;`, `&&`, redirects, backticks or
  `$(...)` is not auto-approved, so `shell:git status*` does not approve `git status; rm -rf ~`. Deny rules are
  checked against each segment of a compound command. Rules are pattern matches; they do not limit what a
  command can do once it runs.
- `AskUser` holds a lock so parallel tool calls never show two prompts at once.
- Starting a language server is its own call, `lsp_start` with `{"server": <name>}`, made by the tool that needs
  the server.
- Every decision goes through the gate's observer and is emitted as `approval_resolved` with `decided_by`.
  Approval events carry the `call_id` of their `tool_call`, because parallel calls can be decided in any order.

### Shell assessment

[core/assess.py](src/jimbo/core/assess.py). `AskUser` calls an assess hook just before prompting, so only calls
that need a human cost a model request (mode `always` also covers allowed calls via the gate observer). The
model's risk is combined with a deterministic floor by `max`. The result is on `ApprovalRequest.assessment` and
the `approval_requested` / `approval_resolved` events.

### Containment

These limit what a call can do after it is approved:

1. **Path confinement** ([tools/paths.py](src/jimbo/tools/paths.py)): file tools resolve symlinks and reject
   paths outside the workspace (`PathEscape`). Read-only tools may also read the folders of loaded skills.
2. **Sandbox** ([tools/sandbox.py](src/jimbo/tools/sandbox.py)): `shell` and language servers start processes
   with `spawn(sandbox, argv, ...)` and stop them with `kill(sandbox, proc)`, which take `Deps.sandbox` (a
   `Sandbox`, or None to run directly). The approval prompt states where the command runs (`describe()`).
   - `SeatbeltSandbox`: on macOS, the process is wrapped in `sandbox-exec` with a profile that allows reads
     anywhere and writes only to the workspace, temp and cache dirs plus `sandbox_writable`, and optionally
     blocks outbound network. The OS enforces it. `kill` kills the process group.
   - `ContainerSandbox` ([tools/container_sandbox.py](src/jimbo/tools/container_sandbox.py)), for
     `sandbox = "docker"` or `"podman"`. Both CLIs take the same commands; `probe` reads `<cli> info` into a
     `Runtime` (engine, rootless, SELinux), which adds `--userns=keep-id` for rootless Podman and
     `label=disable` on SELinux Linux hosts. `kind` is the engine. `build_sandbox` checks the runtime, removes
     containers whose jimbo process is gone, and builds or pulls the image, all before the UI starts. The
     container starts at the first `spawn` (`<cli> run -d`, mounts at the
     same paths as on the host, the host user id, no capabilities) and runs `sleep infinity`; each process is
     a `<cli> exec` with a `JIMBO_RUN=<id>` variable. `kill` kills every process in the container whose
     environment has that id, then the local exec client. A `<cli> wait` process notices when the container
     stops, and the next `spawn` reports it and starts a new one. Port forwards are `asyncio` servers on
     `127.0.0.1` that start `<cli> exec -i ... socat` per connection. `close()` closes the forwards and
     removes the container; `main.py`, `/sandbox reset` and eval trials call it. Language servers are looked
     for in the image with one throwaway container (`find_commands`), and get `processId: null`.
3. **Shell limits** ([tools/shell.py](src/jimbo/tools/shell.py)): stdin is empty, a timeout applies
   (model-adjustable up to `shell_timeout_max`), the command runs in its own process group so the whole tree is
   killed on timeout or interrupt, and long output keeps only its head and tail.
4. **Config trust** ([config.py](src/jimbo/config.py)): a workspace `.jimbo/config.toml` cannot set `providers`,
   `model_aliases`, `models`, `tasks`, `secret_files`, keys, URLs, `workspace`, `web_host`, `log_dir`,
   `user_dir`, `approval` / `approval_allow`, `lsp_servers` or `sandbox*`; those are read only from
   `~/.jimbo/config.toml`. Its `approval_deny` rules are added to the user's.

## Extensions

How each of these reaches the model is in [Prompts](#prompts).

- **Project instructions**: `AGENTS.md` / `JIMBO.md` and memory files at user and workspace level.
- **Skills**: folders containing `SKILL.md` (frontmatter `name`, `description`). Each skill is also a
  `/<name>` slash command. Before each input, `refresh_files` reloads skills if a
  `SKILL.md` changed, and registers the skill and bundled commands again in the same order. Agent files are
  reloaded the same way. The `reload_skills` tool and the `/reload` command run the same reload with
  `force=True` and report which skills changed (`reload_and_describe`); `/reload` also reloads agents.
- **Markdown commands**: `<dir>/<name>.md` files in `~/.jimbo/commands` and `<workspace>/.jimbo/commands`
  become `/<name>`, with `$ARGUMENTS` substitution. With `agent:` in the frontmatter they run in that agent.
- **Agents**: `<dir>/<name>.md` files in `~/.jimbo/agents` and `<workspace>/.jimbo/agents`, plus the built-in
  `explore` and `general` ([core/agents.py](src/jimbo/core/agents.py)). See [Agents](#agents).
- **Bundled files**: `src/jimbo/bundled/` holds agents, skills and commands in the same formats, at the lowest
  precedence. `bundled = false` skips them. `tests/test_bundled.py` checks the real files.
- **Attachments**: `@path` mentions are inlined (capped) for the model while the logged `user_message` keeps
  the original text; pasted images are checked for type and size and dropped with a warning when
  `vision = false`.

## Models and providers

- **Providers**: `"<provider>:<model>"` resolves through [providers/registry.py](src/jimbo/providers/registry.py)
  to a `ProviderConfig` (endpoint plus one credential source, resolved when first needed in
  [providers/credentials.py](src/jimbo/providers/credentials.py)). All current adapters use the
  OpenAI-compatible API. [providers/ratelimit.py](src/jimbo/providers/ratelimit.py) applies a per-provider
  `requests_per_minute` limit on the client. `/model` swaps the agent mid-session and keeps the history.
- **Routing**: [core/models.py](src/jimbo/core/models.py) `ModelRouter` maps a task name (`main`,
  `jimbo-shell-assess`, `jimbo-compaction`, ...) to a model: skill scope (aliases only) > `/models set` >
  `[models]` > the required `main` alias. Agents use `for_agent`: `/models set` > `[models]` > the alias in
  their file > `main`. It lives on `Deps.models`. Background calls go through
  [core/llm_tasks.py](src/jimbo/core/llm_tasks.py) `run_task`, which returns None on failure or timeout.
- **Editing config**: [config_edit.py](src/jimbo/config_edit.py) `ConfigControl` (on `session.config`) handles
  `/model --save`, `/models alias`, `/models set --save`, `/providers` and the web Settings panel. Each change
  reads `~/.jimbo/config.toml` with `tomlkit` (which keeps comments), changes it, validates the edited sections
  against `Settings`, and writes it to a temp file that replaces the original. Then it updates the shared
  `Settings` in place, drops models built for a changed provider (`ModelRouter.forget`), rebuilds the main agent
  if its provider changed, and emits `config`. Only the user file is written.
- A new main model is checked with `inspect_model` before the switch, as at startup. Pasted keys go through
  `store_key` in [providers/credentials.py](src/jimbo/providers/credentials.py): the macOS Keychain (via
  `security -i`, so the key isn't in a process list) or a 0600 file, never `config.toml`. The web panel's
  requests arrive through `runner.config_request`, which refuses changes during a run.

## Agents

An agent run is a child `AgentSession` with `nested=True`, so compaction, cancellation and usage tracking use
the same code as the main loop. `core` can't import `build_agent`, so [wiring.py](src/jimbo/wiring.py) sets
`Deps.spawn_agent`, the only way to start one. `run_agent` calls it from a tool. Agents whose tools are all in
`READ_ONLY_TOOLS` ([tools/agents.py](src/jimbo/tools/agents.py)), and every agent in plan mode, run in parallel
when the model calls several in one response; other agents hold `Deps.agent_lock`, so they run one at a time.
The agents in one response split the requests left (`share`). `spawn_agent` gives a second run of the same agent
the name `<name>-2`, so the ids `ForwardSink` and `Deps.authorize()` put on its calls don't collide.
`AgentSession.delegate()` calls it directly for `@name` and commands with `agent:`.
The child's instructions and messages are in [Agent prompts](#agent-prompts).

- **Deps.** The child gets `replace(parent.deps, ...)` with its own router, empty todos and `agents={}`. Empty
  `agents` stops agents from starting agents. The gate, sandbox and checkpoints are shared, so approvals and
  plan mode apply unchanged. `Deps.agent` adds the agent's name to approval summaries.
- **Checkpoints.** Nested sessions skip `checkpoints.begin()`, so an agent's edits belong to the turn that
  started it, and one `/undo` covers them. `delegate()` calls `begin()` itself because no parent turn is running.
- **Events.** `ForwardSink` passes the child's `tool_call`, `tool_result`, `compaction`, `wrap_up`,
  `instructions` and `error` events to the parent, tagged `agent={name, model}`, and drops the rest. The child's
  thinking, text deltas and `context` are also passed on as progress: the TUI shows them as a status in its
  toolbar, the web UI as a collapsed line per step that expands to the thinking, and plain shows nothing.
- **Models.** Each model an agent uses is checked once with `inspect_model`, and its context window applies to
  the child. An agent on the main model uses the main session's window.
- **Limits.** Child usage is added to the parent's, so `request_limit` is a per-turn total. `run_agent` keeps 5
  requests for the parent. Failures return text instead of raising `ModelRetry`, because a retry could repeat
  file changes that already happened.

## Language servers

[lsp/servers.py](src/jimbo/lsp/servers.py) `resolve_servers` picks one server per language: `lsp_servers` from
the user config first (a built-in name replaces the built-in), then the built-in table, using the first server
whose command `shutil.which` finds.

[lsp/manager.py](src/jimbo/lsp/manager.py) `LspManager` lives on `Deps.lsp`, so agents share it. It holds one
`LspClient` per server, rooted at the workspace, for the whole process; `/clear` and `/resume` keep them, and
`main.py` and the eval runner call `close()` on exit.

- **Start.** `client_for(server, deps, call_id)` starts a server on first use. The first start in a session goes
  through `Deps.authorize` as `lsp_start`, which is not read-only, so the default chain asks. A denial is kept in
  `denied` and not asked about again; `/lsp start` clears it. A server that exits is started again without
  asking, until it has exited 3 times. A server is also restarted when a file it reads only at start changes in
  the workspace root (`RESTART_ON` in servers.py: clangd's `compile_commands.json`, `compile_flags.txt` and
  `.clangd`). The process runs with `start_new_session=True`, through `Sandbox.wrap` when there is a sandbox,
  with stderr in `<log_dir>/<session id>.lsp-<name>.log`.
- **Sync.** `sync(client, path)` sends `didOpen`, or `didChange` and `didSave` with the full text, when the
  file's modification time or size differ from what the server last got. Changes made through `shell` are
  picked up when a tool next uses the file.
- **Client.** [lsp/client.py](src/jimbo/lsp/client.py) reads and writes `Content-Length` framed JSON-RPC with
  plain dicts. It answers the server's requests (`workspace/configuration` from the server's `settings`, the
  rest with empty answers), keeps the latest diagnostics per file, and tracks `$/progress` so a request can wait
  for indexing to end (`wait_idle`). Positions are UTF-16 columns (`utf16_col`, `str_index`).

The tools in [tools/lsp.py](src/jimbo/tools/lsp.py) take a path, a 1-based line and a symbol name; `find_symbol`
turns the name into a column. `Deps.can_use_lsp` hides them when no server was found. Bad arguments raise
`ModelRetry`; a server that is missing, declined or slow returns text, so it does not use up retries.
`project_context` adds a short section naming the languages that have a server.

**Errors after edits.** `write_file`, `edit_file` and `lsp_rename` end with `errors_after_edit`. For a file the
server hasn't seen, it first sends the old text to learn which errors were already there. Then it sends every
changed file, waits up to `lsp_wait` for each file's diagnostics, and lists errors whose message and code
weren't there before. If there are any, it waits up to 0.5 seconds more, because a server can answer before it
has seen the other changed files. If the server stopped checking a file (`BLOCKING` in servers.py: clangd after
a missing header), it adds a note on how to fix it, even when there are no new errors. It never fails the edit.

**Rename.** `lsp_rename` applies the server's `WorkspaceEdit` itself (text edits only; file operations are
refused), after one `authorize` call with the diff of every file. Each file goes through `checkpoints.record`.

## Persistence

- **History**: [core/store.py](src/jimbo/core/store.py) `SessionStore` saves the history and todos after each
  turn to `<id>.messages.json`. `--resume` loads them.
- **Checkpoints**: [core/checkpoints.py](src/jimbo/core/checkpoints.py) records each file as a turn first found
  it. `/diff` shows the changes and `/undo` restores the files. `delete_file` and `move_file` record every file
  in a directory they remove (at most 1,000 files and 50 MB), and `/undo` recreates the directories.
- **Event log**: [core/jsonl.py](src/jimbo/core/jsonl.py) `JsonlSink` writes every event to
  `~/.jimbo/sessions/<id>.jsonl`, which `/trace` opens.

## Testing

`uv run pytest`. Tests build sessions through `build_session()` with a scripted model from `tests/helpers.py`
and assert on emitted events. They cover the loop, approvals, commands and tools without network or UI.

### Evals

[evals/](evals/README.md) runs jimbo on coding tasks with a real model: `uv run python -m evals run`. Each trial
copies a fixture to a temp git repo, builds a session with `build_session()` and an approval chain of `rules`
then allow-all, runs the prompt, and grades the result with a check command (hidden tests copied in after the
run), counts of events, and a judge model (task `evals-judge`). Results, session logs and diffs go to
`evals/results/`. `assess` and `compaction` evaluate the `jimbo-shell-assess` and `jimbo-compaction` tasks.
