# Jimbo reference

Look things up here. To get started, see the [README](../README.md).

- [Configuration](#configuration)
- [All settings](#all-settings)
- [Providers and API keys](#providers-and-api-keys)
- [Models per task](#models-per-task)
- [Approvals](#approvals)
- [Sandbox (macOS)](#sandbox-macos)
- [Interfaces](#interfaces)
- [Slash commands](#slash-commands)
- [Custom commands](#custom-commands)
- [Instructions and skills](#instructions-and-skills)
- [Sessions and context](#sessions-and-context)

## Configuration

Settings are read from these sources, highest priority first:

1. CLI flags
2. `JIMBO_*` environment variables, such as `JIMBO_MODEL` or `JIMBO_WEB_PORT`
3. `<workspace>/.jimbo/config.toml`
4. `~/.jimbo/config.toml` (jimbo doesn't create this file; make it yourself)
5. Defaults

TOML keys are the setting names without the `JIMBO_` prefix.

A workspace config arrives with whatever repo you cloned, so it can't loosen security. These keys are
ignored there, with a warning, and only read from `~/.jimbo/config.toml`: `model`, `api_key`, the
`*_base_url` settings, `providers`, `model_aliases`, `models`, `tasks`, `web_host`, `approval`,
`approval_allow`, `log_dir`, `user_dir`, `workspace` and the `sandbox*` settings. Its `approval_deny` rules
are added to yours.

## All settings

Copy what you need. Values are the defaults unless the comment says otherwise. Top-level keys must come
before the `[tables]`.

```toml
# ---- Model ------------------------------------------------------------------------------------
model = "ollama:llama3.1:8b"  # "<provider>:<name>". Sample value; set this yourself
ui = "plain"                  # plain | tui | web
temperature = 0.2             # sample value; unset by default (the provider's default is used)
request_timeout = 600         # seconds to wait for a model response
request_limit = 50            # max model requests per turn
tool_retries = 3              # how often the model may retry a failed or denied tool call

# ---- Context and compaction -------------------------------------------------------------------
num_ctx = 90000               # context size sent to the model; Ollama's /v1 endpoint ignores it
context_window = 128000       # sample value; your model's real window. Default: num_ctx
compact_threshold = 0.8       # auto-summarize history past this context fill; 0 disables
compact_keep = 6              # recent messages kept verbatim when compacting
vision = true                 # false for text-only models: images are dropped with a warning

# ---- Endpoints for built-in providers (or use [providers.*] below) ----------------------------
ollama_base_url = "http://localhost:11434/v1"
openrouter_base_url = "https://openrouter.ai/api/v1"
# openai_base_url = "..."     # unset: OpenAI's own API

# ---- Approvals --------------------------------------------------------------------------------
approval_allow = []           # see Approvals
approval_deny = []
approval = ["rules", "readonly", "remembered", "ask"]   # policy chain; rarely worth changing

# ---- Shell and sandbox ------------------------------------------------------------------------
shell_timeout = 120           # seconds, per command
shell_timeout_max = 600       # the most the model may ask for
sandbox = "auto"              # auto | on (refuse to run without it) | off
sandbox_writable = []         # extra writable paths
sandbox_network = true        # false blocks outbound network from shell commands

# ---- Web UI -----------------------------------------------------------------------------------
web_host = "127.0.0.1"        # keep on localhost: the page can approve file writes
web_port = 8765
web_open_browser = true

# ---- Sessions ---------------------------------------------------------------------------------
log_sessions = true           # false also disables --resume
log_dir = "/Users/you/.jimbo/sessions"   # default is ~/.jimbo/sessions; write it absolute, "~" isn't expanded

# ---- Model aliases and per-task models (empty by default; sample values) ----------------------
[model_aliases]
fast  = "ollama:llama3.1:8b"
smart = "openrouter:anthropic/claude-sonnet-5-5"

[models]
shell_assess = "fast"
compaction   = "smart"

[tasks.shell_assess]
mode = "auto"                 # off | auto (on once a model is set) | ask | always
timeout = 8                   # seconds before the prompt appears without the assessment

# ---- Providers (none by default; sample values) -----------------------------------------------
[providers.work]
base_url = "https://llm.internal/v1"
api_key_env = "WORK_LLM_KEY"
require_key = true            # fail at startup rather than send a dummy key
send_num_ctx = false          # don't send options.num_ctx to this endpoint
```

## Providers and API keys

Choose a model as `<provider>:<name>`, with `--model`, `JIMBO_MODEL`, `model` in the config, or `/model` in
a session. Every provider speaks the OpenAI-compatible chat API.

| Provider | Default endpoint | API key |
|----------|------------------|---------|
| `ollama` | `http://localhost:11434/v1` | not used |
| `openai` | OpenAI's API | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY`, required |

For hosted models, set `context_window` to the model's real size, because it drives `/usage` and automatic
compaction.

**OpenAI:** turn off the Ollama-style context option, which hosted APIs may reject:

```toml
model = "openai:<model>"
context_window = 128000

[providers.openai]
send_num_ctx = false
```

**Your own endpoint:** a new name under `[providers.*]` adds an OpenAI-compatible endpoint, for LM Studio,
vLLM, a company gateway and so on. Use it as `lmstudio:<model>`. Using a built-in name overrides that
provider instead.

```toml
[providers.lmstudio]
base_url = "http://localhost:1234/v1"
send_num_ctx = false
```

**Keys:** a key is only ever sent to its own provider's endpoint, and never appears in `/models`, events or
logs. `/models` shows where each key comes from. Set at most one source per provider:

| Source | Example | Notes |
|--------|---------|-------|
| `api_key_env` | `"WORK_LLM_KEY"` | environment variable name |
| `api_key_file` | `"~/.jimbo/keys/work"` | must be `chmod 600` |
| `api_key_cmd` | `["pass", "show", "work"]` | run without a shell, 10s timeout, once per process |
| `api_key` | `"sk-..."` | inline; discouraged, and jimbo warns if the file is readable by others |

With none set, `openai` and `openrouter` use the environment variables above, and other providers send a
dummy key. `fallback_key_envs = ["MY_KEY"]` adds more variables to try. `JIMBO_API_KEY` is a legacy option
that works for `openai` only.

To keep a key in the macOS Keychain, store it (this prompts for the value; add `-U` to update it):

```bash
security add-generic-password -a "$USER" -s OPENROUTER_API_KEY -w
```

Then read it from your config, replacing `you` with your username:

```toml
[providers.openrouter]
api_key_cmd = ["security", "find-generic-password", "-a", "you", "-s", "OPENROUTER_API_KEY", "-w"]
```

**Adding a provider in code:** for an API that isn't OpenAI-compatible, implement `ProviderSpec`
([providers/base.py](../src/jimbo/providers/base.py)) and register it in `ADAPTERS` in
[providers/registry.py](../src/jimbo/providers/registry.py). Select it with `type` under `[providers.*]`.

## Models per task

Besides the main conversation, jimbo uses a model for two background tasks: `compaction` and
`shell_assess`. Each can run on a different model, and unset tasks use the main one. In the config, a task
takes an alias, `main`, or a `<provider>:<name>`:

```toml
[model_aliases]
fast = "ollama:llama3.1:8b"

[models]
shell_assess = "fast"
compaction   = "main"
```

In a session, `/models` shows what each task resolves to and why, `/models set <task> <alias|provider:name>`
changes one, and `/models reset [task]` undoes that.

A skill can ask for models in its frontmatter (`models: {shell_assess: smart}`). This lasts one turn and may
only name your aliases, never define a model or endpoint.

**Shell risk assessment:** when `shell_assess` has a model, shell approval prompts show a short summary and
a risk level (low, medium, high or critical). It's advice only. The command is treated as untrusted text,
and fixed checks (`sudo`, `rm -rf`, `curl | sh`, force pushes, writes outside the workspace and so on) set a
minimum risk the model can't lower. If the model is slow or fails, the prompt appears without it. With a
remote model, commands are sent to it, but file contents are not.

`[tasks.shell_assess]` sets `timeout` and `mode`: `off`, `auto` (on once a model is set), `ask` (fall back to
the main model) or `always` (also for calls that are already allowed).

## Approvals

Reading and searching never ask. `write_file`, `edit_file` and `shell` ask first, unless a rule allows them.
At a prompt you can allow a call for the rest of the session, or deny it and give a reason the model will
see.

A rule is `tool` or `tool:<glob>`, matched against the call's first argument (the command or path):

```toml
approval_allow = ["shell:git status*", "shell:uv run pytest*", "write_file:docs/*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*", "read_file:.env*"]
```

- Deny wins, and applies even with `--yolo`.
- An allow rule never matches a compound shell command (pipes, `;`, `&&`, redirects, `$...`), so
  `shell:git status*` can't approve `git status; rm -rf ~`.
- Deny rules are best effort: `bash -c "sudo …"` slips past them. The sandbox is the real boundary.

## Sandbox (macOS)

`shell` runs bash non-interactively in the workspace root, with a timeout. On macOS it runs under
`sandbox-exec`, so commands can read anything but write only to the workspace, temp directories, `~/.cache`
and `~/Library/Caches`. Approval prompts say whether a command is sandboxed. Other platforms have no
sandbox.

- `sandbox`: `auto` uses it when available, `on` refuses to start without it, `off` disables it.
- `sandbox_writable = ["~/other"]` adds writable paths.
- `sandbox_network = false` blocks outbound network.

Git in a workspace that is a subdirectory of a repo can't write the repo's `.git` unless you add it to
`sandbox_writable`. If jimbo itself runs inside a sandbox, `auto` falls back to unsandboxed, because macOS
doesn't allow nesting.

## Interfaces

- **All:** Ctrl+C during a run stops it and keeps the partial turn. A second Ctrl+C within 2 seconds, or one
  at the prompt, quits. Set `vision = false` for text-only models.
- **Steering (`tui`, `web`):** type while the agent works and your message reaches the model at its next
  step. Slash commands are refused mid-run.
- **`tui`:** Enter submits, Shift+Enter or Alt+Enter inserts a newline, and Ctrl+D quits. Pastes over 10 lines
  collapse to `[pasted #1: 240 lines]` and expand on submit. `@path` completes workspace files and inlines
  them for the model. Ctrl+V attaches a clipboard image (needs `pngpaste` or `osascript`).
- **`web`:** serves `http://127.0.0.1:8765` and prints and opens a URL with a one-time token. It accepts
  pasted or dropped images, shows approvals as a dialog with the diff, and turns Send into Stop during a
  run. Keep the host on localhost: the page can approve file writes.

## Slash commands

| Command | |
|---------|--|
| `/help` | list commands |
| `/clear` | forget the conversation so far |
| `/compact [focus]` | summarize older history to free context |
| `/model <provider>:<name>` | show or switch the model |
| `/models` | show or set which model runs each task |
| `/usage` | tokens used and context fill |
| `/approve [yolo\|default]` | show or set the approval mode |
| `/plan [on\|off]` | read-only mode: the agent presents a plan for you to approve |
| `/todos` | show the agent's task list |
| `/skills` | list available skills |
| `/resume [id]` | list saved sessions, or load one |
| `/quit` | exit |

Tab completes commands in `tui` and `web`. Every skill and custom command is also a `/name` command.

## Custom commands

A markdown file in `~/.jimbo/commands/` (every project) or `<workspace>/.jimbo/commands/` (shared with the
repo, and wins on a name clash) becomes a slash command named after the file. For example, `fix-issue.md`:

```markdown
---
description: investigate and fix a bug, with a regression test
---
Fix this bug: $ARGUMENTS

1. Write a failing test that reproduces the bug.
2. Make the smallest fix that makes the test pass.
3. Run the full test suite and report what you changed.
```

`/fix-issue login fails on uppercase emails` sends that prompt with `$ARGUMENTS` replaced. Without a
placeholder, arguments are appended after a blank line. The optional `description` appears in `/help` and
tab completion.

Built-in commands can't be overridden. Names start with a letter and contain only letters, digits, `-` and
`_`.

## Instructions and skills

**Instructions:** `~/.jimbo/AGENTS.md` and the workspace's `AGENTS.md` or `JIMBO.md` are added to the model's
instructions on every request.

**Skills:** a skill is a folder containing a `SKILL.md` with `name` and `description` frontmatter. Jimbo looks
in `.jimbo/skills`, `.claude/skills` and `.agents/skills`, under both your home directory and the workspace.
The model sees each name and description, and loads the body when it needs it.

**Plan mode:** `/plan` allows only read-only tools, and the agent ends with a plan you approve or reject.

**Todos:** for multi-step work the agent keeps a task list, shown in every interface.

## Sessions and context

Each session is logged to `~/.jimbo/sessions/<session_id>.jsonl`, and its history is saved after every turn
so `--resume` can continue it. Change the location with `log_dir`, or turn logging off with
`log_sessions = false`, which also disables resume.

Context fill is measured against `context_window`, which defaults to `num_ctx`. When it passes
`compact_threshold`, older history is summarized automatically, keeping the last `compact_keep` messages.
