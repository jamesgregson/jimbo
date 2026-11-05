# Jimbo

A coding agent for your terminal. It reads and edits files and runs shell commands in your project. It
asks before it writes a file or runs a command.

## Install

You need Python 3.11 or later.

```bash
pip install git+https://github.com/jamesgregson/jimbo.git
```

## Set up a model

Jimbo uses [OpenRouter](https://openrouter.ai) to reach the model. Create an OpenRouter account and an API
key, then run:

```bash
export OPENROUTER_API_KEY=sk-or-...
mkdir -p ~/.jimbo
printf '[model_aliases]\nmain = "openrouter:z-ai/glm-5.3-flash"\n' > ~/.jimbo/config.toml
```

Add the `export` line to your shell profile (for example `~/.zshrc`) so the key is set in every terminal.

To use a different model or provider, see [Providers and API keys](docs/reference.md#providers-and-api-keys).

## Run it

Go to your project and start jimbo:

```bash
cd ~/my-project
jimbo                          # interactive session
jimbo "summarize this repo"    # run one task and exit
```

Other ways to run it:

| Command | What it does |
|---------|--------------|
| `jimbo --ui web` | interactive session in your browser |
| `jimbo --ui plain` | interactive session with plain text output |
| `jimbo --resume` | continue the last session in this project |
| `jimbo --auto` | make file changes, and run shell commands rated low or medium risk, without asking |
| `jimbo --model <name>` | use a different model for this run |
| `cat log.txt \| jimbo "explain this"` | include piped text with the task |

In a session, type `/help` to list commands. Useful ones:

- `/clear` start a new conversation
- `/model` switch model, `/providers` add a provider or set a key
- `/plan` make a plan without changing any files
- `/diff` show the last file changes, `/undo` revert them
- `/trace` see everything that happened in this session, in the browser
- `/quit` exit

Ctrl+C stops the current task.

## Approvals

Jimbo reads files in your project without asking. Before it writes a file or runs a command, it shows
you the change or the command and asks. Answer `y` (yes), `n` (no) or `a` (always). `a` allows that
exact command, or all file writes, for the rest of the session.

To always allow or always refuse some commands, add lines like these to the top of
`~/.jimbo/config.toml`:

```toml
approval_allow = ["shell:git status*", "shell:git diff*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*"]
```

On macOS, commands run in a sandbox that can only write inside your project and temp folders. On other
systems there is no sandbox.

## Project instructions

Put an `AGENTS.md` file in your project root with build commands, test commands and conventions. Jimbo
reads it at the start of each session. Run `/init` to have jimbo write one for you.

## Problems

| Message | Fix |
|---------|-----|
| `jimbo: command not found` | The folder pip installs scripts into is not on your `PATH`. Add it, or install jimbo in a virtual environment and activate it. |
| `no main model` | Create `~/.jimbo/config.toml` as shown in [Set up a model](#set-up-a-model). |
| `provider openrouter has no API key configured` | Set `OPENROUTER_API_KEY` in this terminal, or run `/providers key openrouter` and paste the key. |

## Update

```bash
pip install --force-reinstall git+https://github.com/jamesgregson/jimbo.git
```

`--force-reinstall` is needed because the version number does not change between commits.

## Evals

The eval suite runs jimbo on 25 small coding tasks with a real model and checks the results. Use it to see
whether a change to jimbo, or a different model, does better or worse. It is in the repo, not in the
installed package, so run it from a clone with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/jamesgregson/jimbo.git && cd jimbo
uv run python -m evals run --tag smoke        # 5 tasks, on the main model in ~/.jimbo/config.toml
uv run python -m evals run                    # all tasks
uv run python -m evals run --model <name>     # another model
uv run python -m evals compare <run-a> <run-b>
```

It uses your `~/.jimbo/config.toml` and API key, and each task costs model requests. Each task runs in a temp copy of
its files. On macOS, shell commands run in the sandbox; on other systems, add `--no-sandbox`. Each run saves its results to `evals/results/<run-id>/`. Open
`report.html` there to see each task's result, the judge's verdict, the changes jimbo made and a link to the
trace. [evals/README.md](evals/README.md) covers every option and how to add a task.

## More

[docs/reference.md](docs/reference.md) lists every setting and command, and covers other providers such as
OpenAI and local models with Ollama.
