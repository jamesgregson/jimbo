# Jimbo

A small coding agent for your terminal. It reads and edits files, searches code and runs shell commands
in your project, and asks before doing anything risky. Use it with a local model through Ollama, or with
any OpenAI-compatible service.

## Quick start

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

**1. Install.** This puts a `jimbo` command on your PATH, so you can use it in any project.

```bash
git clone git@github.com:jamesgregson/jimbo.git && cd jimbo
uv tool install --editable .
```

If your shell can't find `jimbo` afterwards, run `uv tool update-shell` and open a new terminal.

**2. Choose a model.** Jimbo has no usable default, so you need to pick one. Choose a model that supports
tool calling.

Local, with [Ollama](https://ollama.com):

```bash
ollama pull llama3.1:8b
mkdir -p ~/.jimbo && echo 'model = "ollama:llama3.1:8b"' >> ~/.jimbo/config.toml
```

This small model is good for checking your setup, but it's unreliable with tools. Use a larger one for real
work.

Or hosted, with [OpenRouter](https://openrouter.ai):

```bash
export OPENROUTER_API_KEY=sk-or-...       # add this to your shell profile to keep it
mkdir -p ~/.jimbo && echo 'model = "openrouter:z-ai/glm-5.3-flash"' >> ~/.jimbo/config.toml
```

**3. Run it** from the project you want to work on:

```bash
cd ~/my-project
jimbo "summarize this repo"     # do one task and exit
jimbo --ui tui                  # interactive session (recommended)
```

Using Ollama for real work? Do [this one extra step](#give-ollama-more-context) first. For OpenAI, LM
Studio or a private endpoint, see [Providers and API keys](docs/reference.md#providers-and-api-keys).

## Using jimbo

| Command | What it does |
|---------|--------------|
| `jimbo "task"` | do one task and exit |
| `jimbo` | interactive session, plain text |
| `jimbo --ui tui` | interactive, with markdown, `@file` completion and image paste |
| `jimbo --ui web` | interactive, in your browser |
| `jimbo --resume [id]` | continue a saved session (the latest if you give no id) |
| `jimbo --model <provider>:<name>` | use a different model for this run |
| `jimbo --workspace <dir>` | work on another directory |

In a session, `/help` lists the commands. The ones you'll want first are `/clear` to start over, `/model`
to switch models, `/plan` to get a read-only plan before any changes, and `/quit`. Ctrl+C stops the current
run and keeps what it has done so far. In `tui` and `web` you can type while it works to steer it.

### What it can do without asking

Reading and searching files in your workspace happens without a prompt. Writing files and running shell
commands ask first, showing you the diff or command. To skip the prompt for commands you trust, or always
refuse others, add rules to `~/.jimbo/config.toml`:

```toml
approval_allow = ["shell:git status*", "shell:git diff*", "shell:uv run pytest*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*", "read_file:.env*"]
```

`--yolo` approves everything except what `approval_deny` blocks.

On macOS, shell commands also run in a sandbox that can only write to your workspace, temp directories and
caches. **There is no sandbox on other platforms**, so read commands carefully before you approve them
there. See [Approvals](docs/reference.md#approvals) and [Sandbox](docs/reference.md#sandbox-macos).

### Give Ollama more context

Ollama ignores the context size jimbo requests, so models run at Ollama's default, often just 4096 tokens.
That's too small for real work, even though jimbo's status line shows `90.0k`. Build a variant of your model
with a larger context:

```bash
printf 'FROM llama3.1:8b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create llama3.1-32k -f Modelfile
```

Then point `~/.jimbo/config.toml` at it, using the same size:

```toml
model = "ollama:llama3.1-32k"
num_ctx = 32768
```

`ollama ps` shows the context a loaded model is really using.

### Teach it about your project

Put an `AGENTS.md` in your project root with your build commands and conventions, and jimbo will follow
them. You can also add [custom slash commands](docs/reference.md#custom-commands) and
[skills](docs/reference.md#instructions-and-skills).

## Troubleshooting

| You see | Fix |
|---------|-----|
| `jimbo: command not found` | Run `uv tool update-shell` and open a new terminal. |
| `Failed to spawn: jimbo` | You used `uv run jimbo` outside the checkout. Install it as in step 1. |
| `model '...' not found` | Run `ollama pull <name>`, or set `model` as in step 2. |
| `provider openrouter has no API key configured` | Set `OPENROUTER_API_KEY` in this shell. |
| The model ignores files or forgets instructions | Its context is probably too small: see [above](#give-ollama-more-context). |
| The model describes tool calls but doesn't make them | Choose a model that supports tool calling. |

## More

- [Reference](docs/reference.md): every setting, plus providers, approvals, the sandbox and customization
- [Architecture](ARCHITECTURE.md): how the code fits together
- [Roadmap](ROADMAP.md): status and what's next

To update, run `git pull` in the checkout, then `uv tool install --editable . --force` if dependencies
changed. To work on jimbo itself, run `uv sync && uv run pytest`.
