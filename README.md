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

**2. Choose a model.** Jimbo won't start until you set a `main` model. Choose one that supports tool calling.
Everything else jimbo uses a model for runs on `main` too, unless you say otherwise.

Local, with [Ollama](https://ollama.com):

```bash
ollama pull llama3.1:8b
mkdir -p ~/.jimbo && printf '[model_aliases]\nmain = "ollama:llama3.1:8b"\n' > ~/.jimbo/config.toml
```

This small model is good for checking your setup, but it's unreliable with tools. Use a larger one for real
work. Jimbo checks at startup that Ollama is running, has the model, and that the model supports tools.

Or hosted, with [OpenRouter](https://openrouter.ai):

```bash
export OPENROUTER_API_KEY=sk-or-...       # add this to your shell profile to keep it
mkdir -p ~/.jimbo && printf '[model_aliases]\nmain = "openrouter:z-ai/glm-5.3-flash"\n' > ~/.jimbo/config.toml
```

**3. Run it** from the project you want to work on:

```bash
cd ~/my-project
jimbo "summarize this repo"     # do one task and exit
jimbo                           # interactive session
```

Using Ollama for real work? Do [this one extra step](#give-ollama-more-context) first. For OpenAI, LM
Studio or a private endpoint, see [Providers and API keys](docs/reference.md#providers-and-api-keys).

## Using jimbo

| Command | What it does |
|---------|--------------|
| `jimbo "task"` | do one task and exit |
| `jimbo` | interactive, with markdown, `@file` completion and image paste |
| `jimbo --ui plain` | interactive, plain text |
| `jimbo --ui web` | interactive, in your browser |
| `cat log \| jimbo "explain this"` | add piped text to the task |
| `jimbo --ui json "task"` | for scripts: one line of JSON per event |
| `jimbo --resume [id]` | continue a saved session (this project's latest if you give no id) |
| `jimbo --model <alias or provider:name>` | use a different main model for this run |
| `jimbo --workspace <dir>` | work on another directory |

In a session, `/help` lists the commands. The ones you'll want first are `/clear` to start over, `/model`
to switch models, `/plan` to get a read-only plan before any changes, `/diff` and `/undo` for the latest
file changes, `/output` for a command's full output, and `/quit`. Ctrl+C stops the current run and keeps what
it has done so far. In `tui` and `web` you can type while it works to steer it.

One-shot tasks exit with a non-zero code if the run fails, so they work in scripts.

### What it can do without asking

Reading and searching files in your workspace happens without a prompt, except secrets like `.env` and
`*.pem`, which the model can't read at all (see [`secret_files`](docs/reference.md#approvals)). Writing files
and running shell commands ask first, showing you the diff or command. Answer `y`, `n` or `a`: anything else
counts as no, and the model gets your text as the reason. To skip the prompt for commands you trust, or
always refuse others, add rules to `~/.jimbo/config.toml`:

```toml
approval_allow = ["shell:git status*", "shell:git diff*", "shell:uv run pytest*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*"]
```

`--yolo` approves everything except what `approval_deny` blocks.

On macOS, shell commands also run in a sandbox that can only write to your workspace, temp directories and
caches. **There is no sandbox on other platforms**, so read commands carefully before you approve them
there. See [Approvals](docs/reference.md#approvals) and [Sandbox](docs/reference.md#sandbox-macos).

### Give Ollama more context

Ollama ignores the context size jimbo requests, so models run at Ollama's default, often just 4096 tokens.
That's too small for real work, and jimbo warns you at startup. Build a variant of your model with a larger
context:

```bash
printf 'FROM llama3.1:8b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create llama3.1-32k -f Modelfile
```

Then point `~/.jimbo/config.toml` at it. Jimbo reads the size from the model:

```toml
[model_aliases]
main = "ollama:llama3.1-32k"
```

`ollama ps` shows the context a loaded model is really using.

### Teach it about your project

Put an `AGENTS.md` in your project root with your build commands and conventions, and jimbo will follow
them. You can also add [custom slash commands](docs/reference.md#custom-commands),
[skills](docs/reference.md#instructions-and-skills) and [agents](docs/reference.md#agents).

## Troubleshooting

| You see | Fix |
|---------|-----|
| `jimbo: command not found` | Run `uv tool update-shell` and open a new terminal. |
| `can't reach Ollama` | Start it with `ollama serve`, or open the Ollama app. |
| `doesn't support tool calling` | Choose another model. |
| `Failed to spawn: jimbo` | You used `uv run jimbo` outside the checkout. Install it as in step 1. |
| `no main model` | Set `main` as in step 2. |
| `provider openrouter has no API key configured` | Set `OPENROUTER_API_KEY` in this shell. |
| The model ignores files or forgets instructions | Its context is probably too small: see [above](#give-ollama-more-context). |
| The model describes tool calls but doesn't make them | Choose a model that supports tool calling. |

## More

- [Reference](docs/reference.md): every setting, plus providers, approvals, the sandbox and customization
- [Architecture](ARCHITECTURE.md): how the code fits together

To update, run `git pull` in the checkout, then `uv tool install --editable . --force` if dependencies
changed. To work on jimbo itself, run `uv sync && uv run pytest`.
