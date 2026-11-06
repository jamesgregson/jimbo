# Jimbo

A coding agent for your terminal. You describe a task, and jimbo reads your code, edits files, runs commands
and tests, and reports what it did. It asks before it changes a file, runs a command or fetches a web page.

It works with any model that supports tool calling, through OpenRouter, OpenAI or a compatible API, or a local
model with Ollama. It runs in a terminal, in a browser, or as a JSON event stream for scripts.

## What you can use it for

- **Fix bugs.** Give it an error, a failing test or a description. It reproduces the problem, finds the cause,
  fixes it and runs the tests.
- **Add features and refactor.** It edits several files, renames symbols across the project with a language
  server, moves and deletes files, and checks each edit for new errors.
- **Write tests** in the style the project already uses.
- **Understand code.** Ask how something works or where something is used. It searches and reads the code and
  answers with file paths.
- **Review changes.** `/review` checks your uncommitted changes for bugs.
- **Plan first.** `/plan` makes it investigate without changing anything and present a plan for you to approve.
- **Run and check things.** It runs builds, tests and git. It can start a dev server in the background, read
  its output and stop it.
- **Look things up.** It reads documentation, changelogs and issues on the web.
- **Automate.** `jimbo "<task>"` runs one task and exits. `--ui json` prints events as JSON lines for scripts
  and CI.

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

To use a different model or provider, including OpenAI and local models with Ollama, see
[Providers and API keys](docs/reference.md#providers-and-api-keys).

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
| `jimbo --ui json "<task>"` | run one task and print events as JSON lines, for scripts. Calls that would ask are refused |
| `jimbo --resume` | continue the last session in this project |
| `jimbo --resume <id>` | continue a saved session by id or the start of its id |
| `jimbo --auto` | make file changes, and run shell commands rated low or medium risk, without asking |
| `jimbo --yolo` | do everything without asking |
| `jimbo --model <name>` | use a different model for this run |
| `jimbo --sandbox docker` | run commands in a Docker container for this run (`podman` for Podman; see [Sandbox](#sandbox)) |
| `jimbo --workspace <dir>` | work on another folder instead of the current one |
| `cat log.txt \| jimbo "explain this"` | include piped text with the task |

## What jimbo can do

The model works through these tools:

| Area | Tools |
|------|-------|
| Read and search | read files (and images), list directories, find files by name, search file contents |
| Change files | write a file, replace text (one or several edits at once), delete and move files or directories |
| Commands | run shell commands with a timeout, or in the background with output read later |
| Code navigation | go to definition, find references, list symbols, show errors, rename a symbol everywhere (needs a [language server](#language-servers)) |
| Web | fetch a page as markdown |
| Images | show you an image, such as a chart it made with a script (`--ui web` only) |
| Work in steps | keep a todo list, ask you questions, hand a task to an [agent](#agents), present a plan |
| Memory | save a note that later sessions read |

Every file change is recorded, so `/diff` shows it and `/undo` reverts it. Shell commands are not recorded.

## In a session

- Type a task and press Enter. While jimbo works, you can type another message and it reaches the model at
  its next step (`tui` and `web`).
- `@path` adds a file to your message. `@shot.png` attaches an image, and `/paste` attaches the clipboard
  image (`tui`, macOS).
- Ctrl+C stops the current task and keeps what it did so far.

Useful commands (type `/help` for all of them):

- `/clear` start a new conversation
- `/model` switch model, `/providers` add a provider or set a key
- `/plan` make a plan without changing any files
- `/diff` show the last file changes, `/undo` revert them
- `/review` review your uncommitted changes
- `/approve auto` or `/approve default` change the approval mode
- `/compact` summarize older history to free context (jimbo also does this when the context fills)
- `/usage` tokens used and estimated cost
- `/skills`, `/agents` list skills and agents
- `/trace` see everything that happened in this session, in the browser
- `/lsp` list the language servers jimbo found
- `/sandbox` show where commands run; `/sandbox port 5173` opens a port in the container sandbox
- `/quit` exit

Each session is saved, so `jimbo --resume` continues it later.

## Approvals and safety

Jimbo reads files in your project without asking. Before it writes, deletes or moves a file, runs a command
or fetches a web page, it shows you what it will do and asks. Answer `y` (yes), `n` (no) or `a` (always). `a`
allows that exact command, that tool's file changes, or fetches from that domain, for the rest of the session.

To always allow or always refuse some commands, add lines like these to the top of
`~/.jimbo/config.toml`:

```toml
approval_allow = ["shell:git status*", "shell:git diff*"]
approval_deny  = ["shell:sudo *", "shell:rm -rf*"]
```

- File tools only work inside the project folder.
- Files such as `.env`, `*.pem` and `*.key` can't be read or searched by the model.
- Commands you approve run in a sandbox, if one is available. See [Sandbox](#sandbox).

See [Approvals](docs/reference.md#approvals).

## Sandbox

A sandbox limits what a command can do after you approve it. The approval prompt and the status line say
where commands run. Language servers run in the same sandbox.

| Setting | Where commands run | Commands can read | Commands can write |
|---------|--------------------|-------------------|--------------------|
| `sandbox = "auto"` (default), on macOS | under `sandbox-exec` | everything | the project, temp folders and caches |
| `sandbox = "auto"` (default), elsewhere | with no sandbox | everything | everything |
| `sandbox = "docker"` | in a Docker container, on macOS and Linux | the project only | the project only |
| `sandbox = "podman"` | in a Podman container, on macOS and Linux | the project only | the project only |
| `sandbox = "off"` | with no sandbox | everything | everything |

The container sandboxes also keep your home folder and environment variables, such as API keys, away from
commands. Docker and Podman work the same way. To use Docker, start it and add this line to the top of
`~/.jimbo/config.toml`:

```toml
sandbox = "docker"
```

For Podman, use `sandbox = "podman"`. On macOS, run `podman machine init` once and `podman machine start` before
starting jimbo.

For one run only, start jimbo with `jimbo --sandbox docker` or `jimbo --sandbox podman`. `--sandbox` takes the same values as the setting
and replaces it for that run.

- The first time, jimbo builds the image `jimbo-sandbox`, which takes a few minutes. It has Debian, git,
  build tools, Python with uv and pytest, Node.js, and the `clangd` and `basedpyright` language servers.
  `jimbo sandbox build` builds it again, for example after you update jimbo (`jimbo sandbox build podman` for
  Podman, which keeps its own images).
- jimbo starts one container at the first command and removes it when jimbo exits. Commands in a session share
  it, so a server started in the background can be reached by later commands.
- Commands run as your user, not root, so they can't install system packages. For other tools, build your own
  image from [the bundled Dockerfile](src/jimbo/bundled/sandbox/Dockerfile) and set `sandbox_image`.
- To open a server in the container from your browser, type `/sandbox port 5173` and go to
  `http://localhost:5173`.
- `sandbox_readonly = ["~/.gitconfig"]` adds a file or folder the container can read, for example so git
  commits have your name.
- `sandbox_network = false` blocks network access in either sandbox.

A sandbox doesn't protect the project itself: an approved command can still change or delete its files.
`/undo` restores file changes jimbo made with its file tools, but not changes made by commands.

See [Sandbox](docs/reference.md#sandbox) for all settings.

## Language servers

If a language server is installed, jimbo uses it to find definitions and references, list symbols and
rename across files. After each edit it tells the model about errors the edit introduced. jimbo asks
before it starts a server.

| Language | Servers |
|---|---|
| Python | `basedpyright`, `pyright` or `pylsp` |
| C, C++ | `clangd` |
| Rust | `rust-analyzer` |
| Go | `gopls` |
| TypeScript, JavaScript | `typescript-language-server` |

With the Docker sandbox, servers run in the container, so jimbo uses the ones in the image, not the ones on your
computer. `/lsp` shows which servers jimbo found. See [Language servers](docs/reference.md#language-servers) to add
another language or turn them off.

## Customize it

**Project instructions:** put an `AGENTS.md` (or `JIMBO.md`) file in your project root with build commands,
test commands and conventions. Jimbo reads it at the start of each session. Run `/init` to have jimbo write
one for you. Instructions for every project go in `~/.jimbo/AGENTS.md`.

**Memory:** tell jimbo to remember something ("remember that tests need `make check`"). It saves the note to
`.jimbo/memory.md` for this project, or `~/.jimbo/memory.md` for every project, and reads it in later sessions.

**Skills:** a skill is a folder with a `SKILL.md` file that tells jimbo how to do one kind of task. Put skills
in `.jimbo/skills/` in your project or in `~/.jimbo/skills/`. Jimbo also reads `.claude/skills/` and
`.agents/skills/`. It loads a skill when the task needs it, and each skill is also a `/name` command.

**Commands:** a markdown file in `.jimbo/commands/` or `~/.jimbo/commands/` becomes a slash command that sends
its text as a prompt.

**Agents:** an agent is a helper with its own instructions, tools and model. It does one task in its own
conversation and returns a report, so its file reads stay out of your main conversation. Read-only agents run
at the same time. Define your own in `.jimbo/agents/<name>.md`, run one with `@name <task>`, or let jimbo
start one.

Jimbo comes with these:

| Kind | Name | What it does |
|------|------|--------------|
| Agent | `explore` | searches and reads code to answer a question. Read-only |
| Agent | `general` | does multi-step work, including edits and commands |
| Agent | `reviewer` | reviews a diff for bugs |
| Agent | `charter` | writes `specs/charter.md`: what the project is and how mature it is |
| Agent | `critic` | reviews a requirements doc for features the project doesn't need yet |
| Command | `/review` | runs `reviewer` on your uncommitted changes |
| Command | `/init` | writes or updates `AGENTS.md` |
| Skill | `debug` | find the cause of a bug or failing test before changing code |
| Skill | `write-tests` | write tests in the style the project already uses |
| Skill | `grill-me` | asks you about a plan, one question at a time, until every decision is settled |
| Skill | `jot-that-down` | saves something you want remembered across sessions |

See [Instructions and skills](docs/reference.md#instructions-and-skills) and [Agents](docs/reference.md#agents).

## Problems

| Message | Fix |
|---------|-----|
| `jimbo: command not found` | The folder pip installs scripts into is not on your `PATH`. Add it, or install jimbo in a virtual environment and activate it. |
| `no main model` | Create `~/.jimbo/config.toml` as shown in [Set up a model](#set-up-a-model). |
| `provider openrouter has no API key configured` | Set `OPENROUTER_API_KEY` in this terminal, or run `/providers key openrouter` and paste the key. |
| `sandbox = "docker" but Docker is not available` | Start Docker (Docker Desktop on macOS), or run without it: `jimbo --sandbox auto`. |
| `sandbox = "podman" but Podman is not available` | On macOS, run `podman machine start` (and `podman machine init` the first time). Or run without it: `jimbo --sandbox auto`. |
| HTTP 429 from the provider | The provider limits requests per minute. Set `requests_per_minute` for that provider in `~/.jimbo/config.toml`. See [Providers and API keys](docs/reference.md#providers-and-api-keys). |

## Update

```bash
pip install --force-reinstall git+https://github.com/jamesgregson/jimbo.git
```

`--force-reinstall` is needed because the version number does not change between commits. For the same reason,
jimbo doesn't notice when the sandbox image is out of date: if you use a container sandbox, run
`jimbo sandbox build` (or `jimbo sandbox build podman`) after updating.

## Develop

To work on jimbo itself, clone the repo and use [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/jamesgregson/jimbo.git && cd jimbo
uv sync                                  # create .venv with jimbo and the dev dependencies
uv run jimbo                             # run jimbo from the source in this clone
uv run jimbo --workspace ~/my-project    # run it on another project
uv run pytest                            # run the tests (no network or API key needed)
uv run jimbo sandbox build               # build the sandbox image, so the Docker tests run instead of being skipped
uv run jimbo sandbox build podman        # the same for the Podman tests
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes how the code is organized.

## Evals

The eval suite runs jimbo on 38 small coding tasks with a real model and checks the results. Use it to see
whether a change to jimbo, or a different model, does better or worse. It is in the repo, not in the
installed package, so run it from a clone (see [Develop](#develop)):

```bash
uv run python -m evals run --tag smoke        # 5 tasks, on the main model in ~/.jimbo/config.toml
uv run python -m evals run                    # all tasks
uv run python -m evals run --tag cpp          # only the C++ tasks
uv run python -m evals run --tag skills       # only the tasks that check skills are used
uv run python -m evals run --model <name>     # another model
uv run python -m evals run --no-lsp           # without language servers
uv run python -m evals run --sandbox docker   # each task in its own Docker container (or --sandbox podman)
uv run python -m evals compare <run-a> <run-b>
```

It uses your `~/.jimbo/config.toml` and API key, and each task costs model requests. The C++ tasks need `make`
and a C++17 compiler (`c++`, or the one in `CXX`). Each task runs in a temp copy of its files. On macOS, shell
commands run under `sandbox-exec`; on other systems, add `--sandbox docker`, or `--no-sandbox` to run them
unconfined. Tasks run 4 at a time; if the provider
returns HTTP 429, add `--requests-per-minute N` or `--jobs 1`.

Each run saves its results to `evals/results/<run-id>/`. Open `report.html` there to see each task's result,
the judge's verdict, the changes jimbo made and a link to the trace. [evals/README.md](evals/README.md) covers
every option and how to add a task.

## More

[docs/reference.md](docs/reference.md) lists every setting and command, and covers other providers such as
OpenAI and local models with Ollama.
