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
- [Bundled files](#bundled-files)
- [Sessions and context](#sessions-and-context)

## Configuration

Settings are read from these sources, highest priority first:

1. CLI flags
2. `JIMBO_USER_DIR`, which replaces `~/.jimbo`. It's the only environment variable jimbo reads
3. `<workspace>/.jimbo/config.toml`
4. `~/.jimbo/config.toml` (jimbo doesn't create this file; make it yourself)
5. Defaults

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
ui = "auto"                   # auto (tui in a terminal, otherwise plain) | tui | plain | web | json
temperature = 0.2             # sample value; unset by default (the provider's default is used)
request_timeout = 600         # seconds to wait for a model response
request_limit = 50            # max model requests per turn; the model is told to wrap up for the last 3
tool_retries = 3              # how often the model may retry a failed or denied tool call
bundled = true                # false loads no bundled agents, skills or commands

# ---- Context and compaction -------------------------------------------------------------------
num_ctx = 90000               # context size sent to the model; Ollama's /v1 endpoint ignores it
context_window = 128000       # sample value; your model's real window. Default: what the server reports, else num_ctx
compact_threshold = 0.8       # auto-summarize history past this context fill; 0 disables
compact_keep = 6              # recent messages kept verbatim when compacting
vision = true                 # false for text-only models: images are dropped with a warning. Ollama reports this itself

# ---- Endpoints for built-in providers (or use [providers.*] below) ----------------------------
ollama_base_url = "http://localhost:11434/v1"
openrouter_base_url = "https://openrouter.ai/api/v1"
# openai_base_url = "..."     # unset: OpenAI's own API

# ---- Approvals --------------------------------------------------------------------------------
approval_allow = []           # see Approvals
approval_deny = []
approval = ["rules", "readonly", "remembered", "ask"]   # policy chain; rarely worth changing
secret_files = [".env", ".env.local", "*.pem", "*.key", "id_rsa"]   # shortened here; see Approvals

# ---- Shell and sandbox ------------------------------------------------------------------------
shell_timeout = 120           # seconds, per command
shell_timeout_max = 600       # the most the model may ask for
sandbox = "auto"              # auto | on (refuse to run without it) | off
sandbox_writable = []         # extra writable paths
sandbox_network = true        # false blocks outbound network from shell commands

# ---- Web UI -----------------------------------------------------------------------------------
web_host = "127.0.0.1"        # keep on localhost: the page can approve file writes
web_port = 8765               # tried first; if it's taken, jimbo uses the next free port
web_open_browser = true

# ---- Sessions ---------------------------------------------------------------------------------
log_sessions = true           # false also disables --resume
log_dir = "/Users/you/.jimbo/sessions"   # default is ~/.jimbo/sessions; write it absolute, "~" isn't expanded

# ---- Model aliases and per-task models (sample values; `main` is required) ---------------------
[model_aliases]
main  = "ollama:llama3.1:8b"  # "<provider>:<name>"; there is no default, jimbo won't start without it
fast  = "ollama:llama3.1:8b"
smart = "openrouter:anthropic/claude-sonnet-5-5"

[models]
jimbo-shell-assess = "fast"
jimbo-compaction   = "smart"

[tasks.jimbo-shell-assess]
mode = "ask"                  # off | ask | always
timeout = 30                  # seconds before the prompt appears without the assessment

# ---- Providers (none by default; sample values) -----------------------------------------------
[providers.work]
base_url = "https://llm.internal/v1"
api_key_env = "WORK_LLM_KEY"
require_key = true            # fail at startup rather than send a dummy key
send_num_ctx = true           # send Ollama-style options.num_ctx; off except for the ollama provider
```

## Providers and API keys

Set the main model as `main = "<provider>:<name>"` under `[model_aliases]`. `--model` overrides it for one run
and `/model` for the session; both also take an alias. `/model <value> --save` also writes it to the config. Every provider speaks the OpenAI-compatible chat API.

| Provider | Default endpoint | API key |
|----------|------------------|---------|
| `ollama` | `http://localhost:11434/v1` | not used |
| `openai` | OpenAI's API | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY`, required |

At startup jimbo checks the main model against its server: it stops if the server is unreachable, rejects
your key, or (with Ollama) doesn't have the model or the model can't call tools. It also takes the context
size from the server where it can (Ollama and OpenRouter). Otherwise set `context_window` to the model's real
size, because it drives `/usage` and automatic compaction. `/model` runs the same check before it switches, and
leaves the model unchanged if the check finds a problem.

**Ollama:** pull a model that supports tool calling, then set it as `main`:

```bash
ollama pull llama3.1:8b
```

```toml
[model_aliases]
main = "ollama:llama3.1:8b"
```

Ollama ignores the context size jimbo requests, so models run at Ollama's default, often 4096 tokens. That
is too small for real work, and jimbo warns at startup. Make a copy of the model with a larger context and
use that instead. Jimbo reads the size from the model. `ollama ps` shows the size a loaded model is using.

```bash
printf 'FROM llama3.1:8b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create llama3.1-32k -f Modelfile
```

```toml
[model_aliases]
main = "ollama:llama3.1-32k"
```

**OpenAI:**

```toml
context_window = 128000

[model_aliases]
main = "openai:<model>"
```

**Your own endpoint:** a new name under `[providers.*]` adds an OpenAI-compatible endpoint, for LM Studio,
vLLM, a company gateway and so on. Use it as `lmstudio:<model>`. Using a built-in name overrides that
provider instead.

```toml
[providers.lmstudio]
base_url = "http://localhost:1234/v1"
```

**Keys:** a key is only ever sent to its own provider's endpoint, and never appears in `/models`, events or
logs. `/models` shows where each key comes from. Set at most one source per provider:

| Source | Example | Notes |
|--------|---------|-------|
| `api_key_env` | `"WORK_LLM_KEY"` | environment variable name |
| `api_key_file` | `"~/.jimbo/keys/work"` | must be `chmod 600` |
| `api_key_cmd` | `["pass", "show", "work"]` | run without a shell, 10s timeout, once per process |
| `api_key` | `"sk-..."` | inline; discouraged, and jimbo warns if the file is readable by others |

With none set, `openai` and `openrouter` use the environment variables above and refuse to start without
them, and other providers send a dummy key unless `require_key = true`. `fallback_key_envs = ["MY_KEY"]` adds more variables to try. The top-level `api_key` setting is a legacy
option that works for `openai` only.

To keep a key in the macOS Keychain, store it (this prompts for the value; add `-U` to update it):

```bash
security add-generic-password -a "$USER" -s OPENROUTER_API_KEY -w
```

Then read it from your config, replacing `you` with your username:

```toml
[providers.openrouter]
api_key_cmd = ["security", "find-generic-password", "-a", "you", "-s", "OPENROUTER_API_KEY", "-w"]
```

**Changing providers in jimbo:** these commands save to `~/.jimbo/config.toml` and apply at once. Your comments
and the order of the file are kept. Other running jimbo sessions see the change when they restart.

| Command | |
|---------|--|
| `/providers` | list providers, their URLs and where each key comes from |
| `/providers add <name> <url>` | add an OpenAI-compatible endpoint; asks for a key, then checks the endpoint |
| `/providers url <name> <url>` | change a provider's URL |
| `/providers key <name>` | set a key; you paste it at a prompt that doesn't show it or keep it in history |
| `/providers key <name> $ENV_VAR` | read the key from an environment variable |
| `/providers rm <name>` | remove a provider, or your settings for a built-in one |
| `/providers test <name>` | check the provider answers and count its models |

At the key prompt, press Enter for no key, or type `$NAME` for an environment variable. A pasted key is not
written to `config.toml`. On macOS it goes in the login Keychain as `jimbo-<provider>`, and the config gets the
matching `api_key_cmd`. Elsewhere it goes in `~/.jimbo/keys/<provider>` (mode 600), and the config gets
`api_key_file`. Removing the provider, or switching it to `$ENV_VAR`, deletes the stored key. `rm` refuses while
an alias or `[models]` entry uses the provider. The `json` UI can't ask for a hidden key, so use `$ENV_VAR` there.

In `web`, the Settings button in the header opens the same controls, with a Models tab and a Providers tab.

**Adding a provider in code:** for an API that isn't OpenAI-compatible, implement `ProviderSpec`
([providers/base.py](../src/jimbo/providers/base.py)) and register it in `ADAPTERS` in
[providers/registry.py](../src/jimbo/providers/registry.py). Select it with `type` under `[providers.*]`.

## Models per task

Besides the main conversation, jimbo uses a model for two background tasks: `jimbo-compaction` and
`jimbo-shell-assess`. Both use `main` unless you choose otherwise. In the config, a task takes an alias, `main`, or a
`<provider>:<name>`:

```toml
[model_aliases]
main = "openrouter:z-ai/glm-5.3-flash"
fast = "ollama:llama3.1:8b"

[models]
jimbo-shell-assess = "fast"
jimbo-compaction   = "main"
```

In a session, `/models` shows what each task resolves to and why, `/models set <task> <alias|provider:name>`
changes one, and `/models reset [task]` undoes that. These last for the session. Add `--save` to `set` or
`reset <task>` to write the change to `[models]` in `~/.jimbo/config.toml`.

`/models alias <name> <provider>:<name>` adds or changes an alias and saves it. `/models alias <name> --rm`
removes it; this is refused while `[models]` or a session choice uses it. Agents whose file names a removed alias
use `main`.

In `tui`, Tab completes model values after `/model`, `/models set <task>` and `/models alias <name>`: aliases,
provider names, and then the provider's model names, which jimbo asks the provider for. In `web`, the model in
the status bar is a menu of your aliases and the current provider's models. It changes the model for the session
only; the Settings panel saves.

A skill can ask for models in its frontmatter (`models: {jimbo-shell-assess: smart}`). This lasts one turn and may
only name your aliases, never define a model or endpoint.

**Shell risk assessment:** shell approval prompts show a short summary and a risk level (low, medium, high or
critical), written by the `jimbo-shell-assess` model, which is `main` by default. The model is told that changes
to files in the workspace are low or medium, and that any change to source control state (commits, branches,
tags, the stash, remotes) or to external state (pushes, pull requests, published packages, container images,
cloud and cluster resources, databases, other machines, write requests to a server) is high. The one exception
is a test server, container or database on this machine, reached through localhost. The command is treated as
untrusted text, and fixed checks set a minimum risk the model can't lower. They cover `sudo`, `rm -rf`,
`curl | sh`, writes outside the workspace, and the common commands that change source control or external
state: `git commit`, `checkout`, `reset`, `stash`, `push` and similar, `gh pr create`/`merge` and other `gh`
changes, `npm`/`uv`/`cargo publish`, `docker push`, `kubectl`, `helm`, `terraform`, `aws`/`gcloud`/`az`
changes, SQL that changes data, `ssh`, and `curl`/`wget` POST, PUT, PATCH and DELETE requests to anything other
than localhost. If the model is slow, fails, or doesn't answer in the expected
format after one retry, the prompt says why (for example "no model summary: timed out after 30s") and marks the
risk as coming from the fixed checks only. After 3 failures in a row, jimbo prints a warning once. With a remote model, commands are sent to it, but file contents are not.

`[tasks.jimbo-shell-assess]` sets `timeout` and `mode`: `off`, `ask` (the default) or `always` (also for calls that
are already allowed).

## Approvals

Reading and searching never ask. `write_file`, `edit_file` and `shell` ask first, unless a rule allows them.

At a prompt, answer `y`, `n` or `a`. Only those exact answers (or `yes`, `no`, `always`) count: anything else
denies the call and gives your text to the model as the reason, so a half-typed sentence can't approve
anything. `a` (always) allows that exact shell command for the rest of the session, or the whole tool for
`write_file` and `edit_file`. In `json` mode, or when stdin is closed, calls that need a prompt are denied.

A rule is `tool` or `tool:<glob>`, matched against the call's first argument (the command or path):

```toml
approval_allow = ["shell:git status*", "shell:uv run pytest*", "write_file:docs/*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*", "read_file:secrets/*"]
```

- Deny wins, and applies even with `--yolo`.
- Paths are matched relative to the workspace, however the model spelled them. Deny rules also match the file
  name, so `read_file:*.sql` covers `db/dump.sql`.
- An allow rule never matches a compound shell command (pipes, `;`, `&&`, redirects, `$...`), so
  `shell:git status*` can't approve `git status; rm -rf ~`.
- Deny rules are best effort: `bash -c "sudo …"` slips past them. The sandbox is the real boundary.

**Auto mode:** `jimbo --auto` or `/approve auto` makes file writes and edits without asking, and runs shell
commands without asking when the `jimbo-shell-assess` model rates them low or medium risk. Commands that
change source control or external state are rated high (see Shell risk assessment), so they still ask.
Staging with `git add` is not covered and can run without asking. Deny rules and `secret_files` still apply. The fixed checks can raise the risk but can't approve a command on their
own, so if the model is slow or fails, jimbo asks. Auto mode needs the assessment model: with `[tasks.jimbo-shell-assess]` `mode = "off"`,
`--auto` and `/approve auto` give an error, and file changes are not auto-approved either. `/approve default` turns it off.

`secret_files` lists file names the model can't read or `grep`, even with `--yolo`. The default covers `.env`
and its usual variants (not `.env.example`), `*.pem`, `*.key`, `id_rsa` and `id_ed25519`. Set it in
`~/.jimbo/config.toml` to change it; a workspace config can't. It doesn't stop a shell command you approve,
or an `@path` you type yourself.

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

- **All:** Ctrl+C during a run stops it and keeps the partial turn. In `plain`, a second Ctrl+C within 2 seconds,
  or one at the prompt, quits. In `tui`, press it twice at the prompt, or Ctrl+D. The current model and any plan,
  auto or yolo mode are shown in the `tui` toolbar and at the top of the `web` page. `tui` and `plain` show the end of
  each shell command's output, and `/output` shows all of it. Tool calls made by an agent show its name and model. Calls that skip the prompt still show their diff.
- **Approval mode (`web`):** the Approvals menu in the header, and the same menu in each approval prompt,
  switches between Ask first, Auto and YOLO, like `/approve`. Choosing Auto or YOLO asks you to confirm first. It
  works while the agent is running. A change made from a prompt applies to later calls; you still answer the one on
  screen.
- **Approval mode (`tui`):** `/approve auto` and `/approve yolo` ask you to confirm, and so does starting with
  `--auto` or `--yolo`. If you don't answer `y`, the mode stays as it was, or the session starts in the default
  mode. `plain`, `json` and one-shot runs don't ask.
- **Steering (`tui`, `web`):** type while the agent works and your message reaches the model at its next
  step. Slash commands are refused mid-run.
- **`tui`:** Enter submits, Shift+Enter or Alt+Enter inserts a newline, and Ctrl+D quits. Pastes over 10 lines
  collapse to `[pasted #1: 240 lines]` and expand on submit. `@path` completes workspace files and inlines
  them for the model. Ctrl+V attaches a clipboard image (needs `pngpaste` or `osascript`).
- **`json`:** for scripts. Every event is a line of JSON on stdout, and calls that need a prompt are denied.
- **`web`:** serves on port 8765, or the next free port if that's taken, then opens the page and prints its
  URL in the terminal, so you can get back to it if you close the tab. The URL carries a token. It accepts
  pasted or dropped images, shows approvals as a dialog with the diff, and turns Send into Stop during a
  run. Keep the host on localhost: the page can approve file writes.

## Slash commands

| Command | |
|---------|--|
| `/help` | list commands |
| `/clear` | forget the conversation so far |
| `/compact [focus]` | summarize older history to free context |
| `/model [<alias\|provider:name>] [--save]` | show or switch the model; `--save` writes it to the config |
| `/models` | show or set which model runs each task, and add or remove aliases |
| `/providers` | list, add or change providers and their keys |
| `/usage` | tokens used and context fill |
| `/approve [auto\|yolo\|default]` | show or set the approval mode |
| `/plan [on\|off]` | read-only mode: the agent presents a plan for you to approve |
| `/todos` | show the agent's task list |
| `/skills` | list available skills |
| `/agents [new <name> [--user]]` | list agents, or create one |
| `/bundled` | list the agents, skills and commands shipped with jimbo, with their paths |
| `/resume [id]` | list this project's saved sessions, or load one by id |
| `/trace [id]` | open this session's trace (or another's) in the browser: every event, and what the model was told |
| `/diff` | show the file changes from the latest turn that made any |
| `/undo` | restore the files that turn changed; repeat to go further back |
| `/output` | show the latest shell output or agent report in full |
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

## Bundled files

Jimbo ships these in the package. They work in every project, with no setup:

| Kind | Name | What it does |
|------|------|--------------|
| Agent | `reviewer` | reviews a diff and reports bugs. It doesn't edit files |
| Command | `/review [change]` | runs `reviewer` on uncommitted changes, or on the change you name |
| Command | `/init` | writes or updates `AGENTS.md`, which applies from the next session |
| Skill | `debug` | reproduce a problem, find the cause, fix it, add a test |
| Skill | `write-tests` | write tests in the style the project already uses |

**Replacing one:** create a file with the same name in any of the usual places. Your files always win, including
skills in `~/.claude/skills` and `~/.agents/skills`. A skill and a command share the `/name` space, so your skill
`review` replaces the command `/review`.

**Changing one:** `/bundled` shows each file's path. Copy the file to `.jimbo/` or `~/.jimbo/`, in the matching folder,
and edit the copy. Don't edit the file in the package, because an upgrade overwrites it. The bundled files change
with the jimbo version, and your copies don't.

**Model:** bundled files don't set a model, so `reviewer` runs on `main`. To change it, see [Agents](#agents).

**Approvals:** `reviewer` runs `git diff`, which asks for approval as usual. To skip the prompt, add
`approval_allow = ["shell:git diff*"]` to `~/.jimbo/config.toml`.

**Turning them off:** set `bundled = false` in `~/.jimbo/config.toml`, or in `.jimbo/config.toml` for one project.
`explore` and `general` still work, because they are built into the code.

## Agents

An agent is a helper with its own prompt, tools and model. It starts with an empty conversation, does one task
and returns one report. Its file reads and tool output stay out of your main conversation.

Jimbo includes `explore`, which is read-only, and `general`, which has all tools.

**Running an agent:** the main model can choose an agent for a task, or you can start one yourself:

```
@reviewer check my last change
```

Your text goes straight to the agent, and the report joins your conversation so you can ask follow-up questions.
When the main model starts an agent, it gets the report, and `/output` shows it in full.

`@name` starts an agent only at the start of a message, and only if that agent exists. Anywhere else, `@` is a file
mention. If a file has the same name as an agent, use `@./name` to mention the file.

**Creating an agent:** `/agents new reviewer` creates `.jimbo/agents/reviewer.md`, and `--user` creates
`~/.jimbo/agents/reviewer.md` for all projects. If both exist, the project agent is used. A file with the same
name as a built-in agent replaces it. Changes to agent files apply from your next message.

The file name is the agent's name. It can't be `main` or start with `jimbo-`. Agent files don't need config changes.

```markdown
---
description: Reviews a diff for bugs. Use after non-trivial changes.
model: fast
tools: [read_file, glob, grep, shell, load_skill]
skills: [python-style]
---
You are a code reviewer. Report only real bugs, with file and line.
```

| Field | Meaning | If omitted |
|---|---|---|
| `description` | Tells the main model when to use the agent | Only you can run it |
| `model` | An alias or `main` | See below |
| `tools` | Tools the agent can use | All tools except `run_agent` and `exit_plan` |
| `skills` | Skills loaded when the agent starts | None loaded |

**Choosing a model:** jimbo uses the first of these that is set, then `main`:

1. `/models set reviewer <alias|provider:name>`, for this session
2. `reviewer = "<alias|provider:name>"` under `[models]` in `~/.jimbo/config.toml`
3. `model:` in the agent file, or in the command that started the agent

Agent and command files can only name your aliases or `main`, so a repo can't add a provider or model. If the alias
doesn't exist, jimbo prints a warning and uses `main`.

**Skills:** `skills:` adds a skill's instructions when the agent starts. An agent can also call `load_skill` if it's
in its `tools`. An agent and a skill can share a name: `@name` runs the agent and `/name` runs the skill.

**Commands:** add `agent:` to a [custom command](#custom-commands) to run it in that agent, and `model:` to replace the
agent file's model for that run. Commands without `agent:` run on the main model.

**Permissions:** agents use your approval rules and the sandbox, and prompts show the agent's name, as in
`[reviewer] run: pytest -x`. Agent files can't change approvals. In plan mode, agents can't write files or run shell
commands. `approval_deny = ["run_agent:reviewer"]` stops the main model starting that agent.

**Limits:** agents run one at a time and can't start other agents. An agent uses the turn's `request_limit`. When the
main model starts one, 5 requests are kept for it to finish the turn, and it receives the first 20,000 characters of
the report. Ctrl+C stops the agent and the current run. `/undo` reverts an agent's file edits with the rest of the turn.

**Repo agents run without a trust prompt.** A cloned repo's agent can use any of your aliases, so it could send code
to a hosted model. Each tool call it makes still needs approval.

## Plan mode and todos

**Plan mode:** `/plan` allows only read-only tools, and the agent ends with a plan you approve or reject.

**Todos:** for multi-step work the agent keeps a task list, shown in every interface.

## Sessions and context

Each session is logged to `~/.jimbo/sessions/<session_id>.jsonl`, and its history is saved after every turn
so `--resume` can continue it. `--resume` with no id takes the latest session from the current project, and
an id loads any session. `/clear` starts a new session, so the old one stays resumable. Change the location
with `log_dir`, or turn logging off with `log_sessions = false`, which also disables resume.

`/undo` only covers `write_file` and `edit_file`, not shell commands, and only for the current run of jimbo.

Context fill is measured against `context_window`, which defaults to `num_ctx`. When it passes
`compact_threshold`, older history is summarized automatically, keeping the last `compact_keep` messages.
