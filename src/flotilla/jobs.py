"""Background jobs whose output the web UI streams."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

LOG_DIR = Path("~/.config/flotilla/jobs").expanduser()
_ids = itertools.count(1)


@dataclass
class Job:
    id: str
    title: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"          # running | ok | failed
    lines: list[str] = field(default_factory=list)
    result: Any = None

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status,
                "started": self.started, "finished": self.finished,
                "result": self.result, "lines": len(self.lines)}


class Jobs:
    """Thread-backed job registry. Logs also go to ``~/.config/flotilla/jobs``."""

    def __init__(self, keep: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._keep = keep
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[dict[str, Any]]:
        return [j.summary() for j in
                sorted(self._jobs.values(), key=lambda j: -j.started)]

    def start(self, title: str,
              work: Callable[[Callable[[str], None]], Iterable[Any] | Any]
              ) -> Job:
        """Run ``work(emit)`` in a thread.

        ``work`` may return an iterable: strings become log lines,
        ``("result", value)`` sets the job result, ``("exit", code)``
        sets the status.
        """
        job = Job(id=f"{int(time.time())}-{next(_ids)}", title=title)
        with self._lock:
            self._jobs[job.id] = job
            for old in sorted(self._jobs.values(),
                              key=lambda j: j.started)[:-self._keep]:
                self._jobs.pop(old.id, None)
        log = (LOG_DIR / f"{job.id}.log").open("a")

        def emit(line: str) -> None:
            for part in line.rstrip("\n").split("\n"):
                job.lines.append(part)
                log.write(part + "\n")
            log.flush()

        def runner() -> None:
            code = 0
            try:
                out = work(emit)
                if out is not None and not isinstance(out, (str, dict)):
                    for item in out:
                        if isinstance(item, str):
                            emit(item)
                        elif item[0] == "exit":
                            code = item[1]
                        else:
                            job.result = item[1]
            except Exception as exc:  # noqa: BLE001 - shown to the user
                emit(f"error: {exc}")
                emit(traceback.format_exc(limit=3))
                code = 1
            job.status = "ok" if code == 0 else "failed"
            job.finished = time.time()
            emit(f"--- {job.status} ({job.finished - job.started:.1f}s)")
            log.close()

        threading.Thread(target=runner, daemon=True).start()
        return job
