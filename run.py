"""
Pipeline runner with auto-restart and hourly status.

Usage:
    python run.py             # tag + embed
    python run.py --tag-only  # tag only
"""

import sys
import io
import time
import os

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import json
import subprocess
from pathlib import Path
from datetime import datetime

TAG_ONLY    = "--tag-only" in sys.argv
LABELS_FILE = Path("output/file_labels.json")
INDEX_FILE  = Path("output/relevant_files.json")
LOG_FILE    = Path("output/pipeline_log.txt")
LOCK_FILE   = Path("output/pipeline.lock")
PYTHON      = str(Path(__file__).resolve().parent / ".venv/Scripts/python.exe")

# ── Single-instance lock ───────────────────────────────────────────────────────
# Prevents duplicate processes (system Python + .venv Python both starting).
# Uses O_EXCL for atomic creation — only the first process wins.
LOCK_FILE.parent.mkdir(exist_ok=True)
try:
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
except FileExistsError:
    # Lock exists — check if the owning PID is still alive
    try:
        pid = int(LOCK_FILE.read_text().strip())
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            print(f"Another instance already running (PID {pid}). Exiting.")
            sys.exit(0)
        # Stale lock (process dead) — overwrite and continue
        LOCK_FILE.write_text(str(os.getpid()))
    except Exception:
        LOCK_FILE.write_text(str(os.getpid()))

import atexit
atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))

MAX_RESTARTS    = 999
STATUS_INTERVAL = 3600   # seconds between status prints


def print_status(run_num=None):
    if not LABELS_FILE.exists():
        print("[status] No output yet.")
        return

    labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    total  = "?"
    if INDEX_FILE.exists():
        all_rel = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        target_exts = {".pdf",".docx",".doc",".pptx",".ppt",".xlsx",".xls",".odt",".ods",".odp"}
        total = sum(1 for f in all_rel if f["ext"].lower() in target_exts)

    done    = len(labels)
    skipped = sum(1 for v in labels.values() if v.get("skipped"))
    errors  = sum(1 for v in labels.values() if "error" in str(v.get("error","")))
    tagged  = done - skipped - errors

    pct = f"{done/total*100:.1f}%" if isinstance(total, int) and total else "?"
    bar_len = 40
    filled  = int(done/total*bar_len) if isinstance(total, int) and total else 0
    bar     = "#" * filled + "-" * (bar_len - filled)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = f"  [Run #{run_num}]" if run_num else ""
    print(f"\n{'='*60}")
    print(f"  Status  {now}{label}")
    print(f"  [{bar}] {pct}")
    print(f"  Processed: {done:,} / {total:,}   Tagged: {tagged:,}   Skipped: {skipped:,}   Errors: {errors:,}")

    recent = [(p, v) for p, v in labels.items()
              if not v.get("skipped") and "error" not in str(v.get("error",""))][-3:]
    if recent:
        print("  Last tagged:")
        for path, v in recent:
            print(f"    [{v.get('document_type','?'):<14}] {v.get('product',''):<14} {Path(path).name[:45]}")
    print(f"{'='*60}\n")


def run_pipeline():
    cmd = [PYTHON, "-u", "embedder/pipeline.py"]
    if TAG_ONLY:
        cmd.append("--tag-only")

    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(f"\n--- Started {datetime.now().isoformat()} ---\n")
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            cwd=str(Path(__file__).parent),
        )
        return proc


mode = "tag-only" if TAG_ONLY else "tag + embed"
print(f"\nAuto-runner started  [{mode}]")
print(f"Status every {STATUS_INTERVAL//60} minutes. Log -> {LOG_FILE}")
print("Press Ctrl-C to stop.\n")

for run_num in range(1, MAX_RESTARTS + 1):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting pipeline (run #{run_num})...")
    proc = run_pipeline()

    last_status = time.time()

    while proc.poll() is None:
        time.sleep(10)
        if time.time() - last_status >= STATUS_INTERVAL:
            print_status(run_num)
            last_status = time.time()

    exit_code = proc.returncode
    print_status(run_num)

    if exit_code == 0:
        print("Pipeline finished successfully.")
        break

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Crashed (exit {exit_code}). Restarting in 10s...")
    time.sleep(10)
