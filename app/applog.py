"""The app's own log, kept on disk so it can be downloaded (Settings -> App logs) to catch problems with the app.

install(name) copies everything the process prints (stdout and stderr) into <data>/logs/<name>.log with a time on
each line, and logs uncaught errors (also in threads) with their traceback. The console still gets everything, so
`docker logs` works as before. Each file is kept under MAX_BYTES: when it grows past that it becomes <name>.log.1
(the one before is dropped), so the logs never take more than about 2 x MAX_BYTES per process.

Nothing private is removed here (the miners' passwords are never printed); the download (fan_addon.logs_zip) takes
addresses, tokens and names out before anything leaves the app.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

MAX_BYTES = 2 * 1024 * 1024
_lock = threading.Lock()
_installed: set[str] = set()


def log_dir() -> Path:
    reg = os.environ.get("SCLITE_WEBUI_MINERS")
    base = Path(reg).parent if reg else Path(os.environ.get("B2AC_DATA", "/data"))
    return base / "logs"


class _Tee(io.TextIOBase):
    """Writes to the original stream and, a line at a time with a time stamp, to the log file."""

    def __init__(self, orig: Any, path: Path):
        self.orig, self.path = orig, path
        self._buf = ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        try:
            self.orig.write(s)
        except Exception:
            pass
        try:
            with _lock:
                self._buf += s
                if "\n" in self._buf:
                    lines, self._buf = self._buf.rsplit("\n", 1)
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    _append(self.path, "".join(f"{stamp} {ln}\n" for ln in lines.split("\n")))
        except Exception:
            pass       # logging must never break the app
        return len(s)

    def flush(self) -> None:
        try:
            self.orig.flush()
        except Exception:
            pass

    def fileno(self) -> int:
        return self.orig.fileno()

    def isatty(self) -> bool:
        return False


def _append(path: Path, text: str) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, text.encode("utf-8", "replace"))
    finally:
        os.close(fd)


def install(name: str) -> Path | None:
    """Start keeping this process's output in <data>/logs/<name>.log (once per process)."""
    if name in _installed:
        return log_dir() / f"{name}.log"
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        return None
    path = d / f"{name}.log"
    _installed.add(name)
    sys.stdout = _Tee(sys.stdout, path)
    sys.stderr = _Tee(sys.stderr, path)

    def hook(et: Any, ev: Any, tb: Any) -> None:
        print("UNCAUGHT ERROR:\n" + "".join(traceback.format_exception(et, ev, tb)), file=sys.stderr, flush=True)

    def thook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        print(f"UNCAUGHT ERROR in thread {getattr(args.thread, 'name', '?')}:\n"
              + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
              file=sys.stderr, flush=True)
    sys.excepthook = hook
    threading.excepthook = thook
    from_ver = os.environ.get("B2AC_VERSION", "?")
    print(f"[log] {name} started (Blake2b ASIC Control {from_ver}, Python {sys.version.split()[0]})", flush=True)
    return path
