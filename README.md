# Jimbo Coding Agent

A simple coding agent on Pydantic AI. The core (`jimbo.core`) is headless: it emits `Event`s and
asks for approvals through a `Frontend` protocol, so UIs are swappable.

    uv sync
    uv run jimbo "summarize this repo"          # one-shot
    uv run jimbo                                # interactive (plain UI)
    uv run jimbo --model ollama:<tag> --yolo

Config via `JIMBO_*` env vars (see `config.py`). Ollama's `/v1` endpoint may ignore `num_ctx`;
bake it into the model's Modelfile.

Status: milestone 1 (provider, session/events, approval policy chain, plain frontend,
`read_file`/`list_dir`). Not yet: write/edit/shell tools, sandbox, JSONL logger/protocol docs.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A model endpoint: [Ollama](https://ollama.com) running locally (default, `http://localhost:11434/v1`)
  or any OpenAI-compatible server

### Install

```bash
git clone git@github.com:jamesgregson/jimbo.git && cd jimbo
uv sync                  # creates .venv and installs runtime + dev dependencies
```

### Configure

Settings live in `src/jimbo/config.py` and are read from `JIMBO_*` environment variables
(e.g. `JIMBO_MODEL`, `JIMBO_OLLAMA_BASE_URL`, `JIMBO_API_KEY`) or overridden with CLI flags.
Local `.env` files are git-ignored; do not commit secrets.

For the default local setup, pull a model and point jimbo at it:

```bash
ollama pull <tag>
uv run jimbo --model ollama:<tag> "summarize this repo"
```

### Secrets on macOS (Keychain)

Store API keys in your keychain rather than in plaintext files:

```bash
# follow interactive prompt to set secret value, add -U to update
security add-generic-password -a "$USER" -s "OPENROUTER_API_KEY" -w
```

Read it back into the environment when launching, e.g.:

```bash
JIMBO_API_KEY="$(security find-generic-password -a "$USER" -s OPENROUTER_API_KEY -w)" uv run jimbo
```

Then select the OpenRouter provider with a `openrouter:` model prefix:

```bash
uv run jimbo --model openrouter:anthropic/claude-sonnet-4.5 "summarize this repo"
```

The `ollama`, `openai` and `openrouter` providers are registered. OpenRouter defaults to
`https://openrouter.ai/api/v1` (override with `JIMBO_OPENROUTER_BASE_URL`) and does not receive `num_ctx`.

### Test

```bash
uv run pytest
```
