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
