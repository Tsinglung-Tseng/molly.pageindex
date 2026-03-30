#!/usr/bin/env python3
"""
Unified supervisor: web_ui + daily batch index + PPID watcher.

Run:
    uv run python main.py [--host 127.0.0.1] [--port 7842]

When PPID becomes 1 (parent died), all services are shut down.
MCP server is NOT managed here — it uses stdio and is launched
separately by Claude Code.
"""

import os
import sys
import select
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

PAGEINDEX_DIR = Path(__file__).parent.resolve()

os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings


# ---------------------------------------------------------------------------
# Parent-death detection — exit instantly when parent dies
# ---------------------------------------------------------------------------

def _get_ppid_of(pid: int) -> int:
    """Return the parent PID of a given PID."""
    import subprocess
    try:
        return int(subprocess.check_output(
            ['ps', '-o', 'ppid=', '-p', str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip())
    except Exception:
        return 0


def _collect_ancestors() -> list[int]:
    """Return [parent, grandparent, ...] up to but excluding PID 0/1."""
    chain = []
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen:
        chain.append(pid)
        seen.add(pid)
        pid = _get_ppid_of(pid)
    return chain


def _watch_ppid():
    """Watch the entire ancestor chain.  If ANY ancestor exits, os._exit().

    No need to guess which process is "the real parent" — just watch them all.
    On macOS uses kqueue (instant); fallback polls with os.kill(pid, 0).
    """
    ancestors = _collect_ancestors()
    if not ancestors:
        print("[main] No ancestors found (orphan at startup), exiting.", flush=True)
        os._exit(0)

    # Log the chain for diagnostics
    import subprocess
    chain_desc = []
    for pid in ancestors:
        try:
            comm = subprocess.check_output(
                ['ps', '-o', 'comm=', '-p', str(pid)],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            chain_desc.append(f"{pid}({os.path.basename(comm)})")
        except Exception:
            chain_desc.append(f"{pid}(?)")
    print(f"[main] Watching ancestor chain: {' → '.join(chain_desc)}", flush=True)

    if sys.platform == 'darwin':
        try:
            kq = select.kqueue()
            events = [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD,
                    fflags=select.KQ_NOTE_EXIT,
                )
                for pid in ancestors
            ]
            kq.control(events, 0)          # register all
            result = kq.control(None, 1)   # block until ANY one exits
            dead_pid = result[0].ident if result else '?'
            print(f"[main] Ancestor {dead_pid} exited (kqueue), shutting down.", flush=True)
            os._exit(0)
        except OSError as e:
            print(f"[main] kqueue failed ({e}), falling back to poll.", flush=True)

    # Fallback: poll all ancestors
    while True:
        time.sleep(1)
        for pid in ancestors:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print(f"[main] Ancestor {pid} exited (poll), shutting down.", flush=True)
                os._exit(0)
            except PermissionError:
                pass


# ---------------------------------------------------------------------------
# Daily batch index scheduler
# ---------------------------------------------------------------------------

def _seconds_until(target_hm: str) -> float:
    """Seconds from now until the next occurrence of HH:MM today or tomorrow."""
    h, m = map(int, target_hm.split(':'))
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _schedule_loop():
    """Run batch_index.main() once per day at the configured time."""
    from batch_index import main as batch_main

    schedule_time = settings.schedule_time
    print(f"[scheduler] Daily batch index scheduled at {schedule_time}", flush=True)

    while True:
        wait = _seconds_until(schedule_time)
        print(f"[scheduler] Next run in {wait/3600:.1f}h", flush=True)
        time.sleep(wait)

        print(f"[scheduler] Starting batch index at {datetime.now():%Y-%m-%d %H:%M}", flush=True)
        try:
            batch_main()
            print("[scheduler] Batch index completed.", flush=True)
        except Exception as e:
            print(f"[scheduler] Batch index error: {e}", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser(description='PageIndex Supervisor')
    p.add_argument('--host', default=settings.web_host)
    p.add_argument('--port', type=int, default=settings.web_port)
    args = p.parse_args()

    # 1) PPID watcher
    threading.Thread(target=_watch_ppid, daemon=True).start()

    # 2) Daily batch index scheduler
    threading.Thread(target=_schedule_loop, daemon=True).start()

    # 3) Web UI (blocks main thread)
    import uvicorn
    from web_ui import app

    print(f"\n  PageIndex Supervisor")
    print(f"  Web UI  →  http://{args.host}:{args.port}")
    print(f"  Schedule →  daily at {settings.schedule_time}\n", flush=True)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
