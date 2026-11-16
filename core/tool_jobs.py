"""Round 63: run a command-line tool (tools/narrate.py, tools/clip_stream.py) as a background job
from the app, and read its progress back.

Why a separate process rather than importing the tools into Streamlit: each tool sets its own
defaults in os.environ when it starts (CAPTION_STYLE=bold, CLIP_LENGTH=gaming, no LinkedIn
copy...). Inside the app's process those would leak into the podcast pipeline. As a job, a tool
runs exactly as it does from the terminal, keeps running when the page reloads or you switch tabs,
and can be stopped.

Each job is a folder under storage/jobs/<id>/:
  meta.json   what was run (kind, label, argv, started, pid)
  log.txt     everything the tool printed
  exit        its exit code, written when it ends (missing while it runs)

This file runs itself as the job's wrapper (python core/tool_jobs.py run <job folder>), so it
imports nothing from the rest of Nav.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "storage" / "jobs"
KEEP_JOBS = 40  # older finished job folders are deleted when a new job starts


@dataclass
class Job:
    id: str
    kind: str
    label: str
    argv: list[str]
    started: float
    pid: int
    folder: Path

    @property
    def log_path(self) -> Path:
        return self.folder / "log.txt"

    def exit_code(self) -> int | None:
        try:
            return int((self.folder / "exit").read_text().strip())
        except (OSError, ValueError):
            return None

    def status(self) -> str:
        """running | done | failed | stopped"""
        code = self.exit_code()
        if code is not None:
            return "done" if code == 0 else "stopped" if code in (-signal.SIGTERM, 128 + signal.SIGTERM) else "failed"
        return "running" if _alive(self.pid) else "stopped"

    def tail(self, lines: int = 40) -> str:
        """The last lines the tool printed. Progress bars that redraw with \\r show their latest state."""
        try:
            text = self.log_path.read_bytes().decode(errors="replace")  # bytes: read_text turns \r into \n
        except OSError:
            return ""
        shown = [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]
        while shown and not shown[-1].strip():
            shown.pop()
        return "\n".join(shown[-lines:])

    def seconds(self) -> float:
        end = (self.folder / "exit").stat().st_mtime if (self.folder / "exit").exists() else time.time()
        return max(0.0, end - self.started)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a finished child of this process stays a zombie until reaped: reap it (non-blocking)
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _load(folder: Path) -> Job | None:
    try:
        meta = json.loads((folder / "meta.json").read_text())
        return Job(meta["id"], meta["kind"], meta["label"], list(meta["argv"]), float(meta["started"]),
                   int(meta.get("pid", 0)), folder)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def start(kind: str, label: str, argv: list[str], jobs_dir: Path | None = None, cwd: Path = ROOT,
          env: dict[str, str] | None = None) -> Job:
    """Start ``argv`` (e.g. [sys.executable, "tools/narrate.py", "library"]) in the background."""
    jobs_dir = Path(jobs_dir or JOBS_DIR)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    _prune(jobs_dir)
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = jobs_dir / job_id
    folder.mkdir()
    meta = {"id": job_id, "kind": kind, "label": label, "argv": [str(a) for a in argv], "started": time.time(),
            "cwd": str(cwd), "pid": 0}
    (folder / "meta.json").write_text(json.dumps(meta, indent=1))
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(folder)], cwd=str(cwd),
                            env={**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    meta["pid"] = proc.pid
    (folder / "meta.json").write_text(json.dumps(meta, indent=1))
    return Job(job_id, kind, label, meta["argv"], meta["started"], proc.pid, folder)


def jobs(kind: str | None = None, jobs_dir: Path | None = None) -> list[Job]:
    """Jobs, newest first (``kind``: only that kind)."""
    root = Path(jobs_dir or JOBS_DIR)
    if not root.is_dir():
        return []
    found = [j for j in (_load(f) for f in root.iterdir() if f.is_dir()) if j and (kind is None or j.kind == kind)]
    return sorted(found, key=lambda j: j.started, reverse=True)


def running(kind: str | None = None, jobs_dir: Path | None = None) -> list[Job]:
    return [j for j in jobs(kind, jobs_dir) if j.status() == "running"]


def stop(job: Job) -> None:
    """Stop the job and everything it started (it runs in its own process group)."""
    if job.status() != "running":
        return
    try:
        os.killpg(job.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _prune(root: Path) -> None:
    import shutil

    finished = [j for j in jobs(None, root) if j.status() != "running"]
    for job in finished[KEEP_JOBS:]:
        shutil.rmtree(job.folder, ignore_errors=True)


def _run(folder: Path) -> int:
    """The wrapper: run the job's command, its output into log.txt, its exit code into exit."""
    meta = json.loads((folder / "meta.json").read_text())
    with open(folder / "log.txt", "ab", buffering=0) as log:
        log.write(f"$ {shlex.join(meta['argv'])}\n".encode())
        try:
            child = subprocess.Popen(meta["argv"], cwd=meta.get("cwd") or None, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        except OSError as exc:
            log.write(f"Could not start: {exc}\n".encode())
            code = 127
        else:
            def forward(signum, _frame):  # Stop: pass it on, then record how it ended
                child.send_signal(signum)

            signal.signal(signal.SIGTERM, forward)
            code = child.wait()
    tmp = folder / "exit.tmp"
    tmp.write_text(str(code))
    os.replace(tmp, folder / "exit")
    return code


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "run":
    raise SystemExit(0 if _run(Path(sys.argv[2])) == 0 else 1)
