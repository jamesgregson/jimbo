"""Append-only JSONL event log, one file per session."""

from pathlib import Path

from .events import Event


class JsonlSink:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._files: dict[str, object] = {}

    async def emit(self, event: Event) -> None:
        f = self._files.get(event.session_id)
        if f is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            f = self._files[event.session_id] = open(self.directory / f"{event.session_id}.jsonl", "a", encoding="utf-8")
        f.write(event.model_dump_json() + "\n")
        f.flush()  # survive crashes and Ctrl+C

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()
