# Jimbo Coding Agent

A simple coding agent on Pydantic AI. The core (`jimbo.core`) is headless: it emits `Event`s and
asks for approvals through a `Frontend` protocol, so UIs are swappable.

    uv sync
    uv run jimbo "summarize this repo"          # one-shot
    uv run jimbo                                # interactive (plain UI)
    uv run jimbo --model <provider>:<model> --yolo
    uv run jimbo --ui tui                       # rich inline UI (streaming markdown)

    uv run jimbo --ui web                       # browser UI on http://127.0.0.1:8765

The `web` UI serves a local page (URL with a one-time token is printed and opened; set
`JIMBO_WEB_HOST`, `JIMBO_WEB_PORT`, `JIMBO_WEB_OPEN_BROWSER=false` to change). Approvals appear as a
dialog with the diff. Keep the host on localhost: the page can approve file writes.

The `tui` UI: Enter submits, Alt+Enter inserts a newline, Ctrl+D (or Ctrl+C twice) quits.

Each session is logged as JSONL events to `~/.jimbo/sessions/<session_id>.jsonl`
(`JIMBO_LOG_DIR` to move it, `JIMBO_LOG_SESSIONS=false` to disable).

Config via `JIMBO_*` env vars (see `config.py`). See [Providers](#providers) for model setup.

Status: milestone 1 (provider, session/events, approval policy chain, plain + tui + web frontends,
`read_file`/`list_dir`/`write_file`/`edit_file`). Not yet: shell tool, sandbox, protocol docs.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A model endpoint from one of the [providers](#providers) below

### Install

```bash
git clone git@github.com:jamesgregson/jimbo.git && cd jimbo
uv sync                  # creates .venv and installs runtime + dev dependencies
```

### Configure

Settings live in `src/jimbo/config.py` and are read from `JIMBO_*` environment variables
(e.g. `JIMBO_MODEL`, `JIMBO_API_KEY`) or overridden with CLI flags. Local `.env` files are
git-ignored; do not commit secrets.

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

## Providers

The model is selected as `<provider>:<model name>`, via `--model` or `JIMBO_MODEL`. All providers
speak the OpenAI-compatible chat API and share `JIMBO_API_KEY`, `JIMBO_TEMPERATURE` and
`JIMBO_REQUEST_TIMEOUT`. Providers are registered in `src/jimbo/providers/registry.py`.

| Provider     | Base URL setting             | Default base URL               | API key  | `num_ctx` |
|--------------|------------------------------|--------------------------------|----------|-----------|
| `ollama`     | `JIMBO_OLLAMA_BASE_URL`      | `http://localhost:11434/v1`    | not used | sent      |
| `openai`     | `JIMBO_OPENAI_BASE_URL`      | none, must be set              | required | sent      |
| `openrouter` | `JIMBO_OPENROUTER_BASE_URL`  | `https://openrouter.ai/api/v1` | required | not sent  |

### ollama

Local models via [Ollama](https://ollama.com). This is the default provider.

```bash
ollama pull <tag>
uv run jimbo --model ollama:<tag> "summarize this repo"
```

Context size is requested with `JIMBO_NUM_CTX` (default 90000), but Ollama's `/v1` endpoint may
ignore it; bake it into the model's Modelfile instead.

### openai

OpenAI itself, or any other OpenAI-compatible server (LM Studio, vLLM, ...). The base URL has no
default, so set it:

```bash
JIMBO_OPENAI_BASE_URL=https://api.openai.com/v1 JIMBO_API_KEY=sk-... \
  uv run jimbo --model openai:<model> "summarize this repo"
```

`JIMBO_NUM_CTX` is sent as an extra body field, which hosted APIs may reject; set it empty if so.

### openrouter

[OpenRouter](https://openrouter.ai) hosted models. Model names include the upstream vendor:

```bash
export JIMBO_API_KEY="$(security find-generic-password -a "$USER" -s OPENROUTER_API_KEY -w)"
uv run jimbo --model openrouter:z-ai/glm-5.3-flash "summarize this repo"
```

### Adding a provider

Add an entry to `PROVIDERS` in `src/jimbo/providers/registry.py`. For an OpenAI-compatible
endpoint, add a base URL setting to `config.py` and register
`OpenAICompatProvider("<setting>", default_base_url=..., send_num_ctx=...)`. Other APIs need a
class implementing `ProviderSpec` (`providers/base.py`).

### Test

```bash
uv run pytest
```
