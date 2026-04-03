"""
Copy embeddable files from R:\ to a local folder, preserving relative paths.

Reads the list of embeddable files from output/relevant_files.json (produced
by scanner/scan.py) and copies each one into DEST_ROOT with the same folder
structure it has under R:\.

The copy is resumable: files already present at the destination with a
matching size are skipped, so you can safely interrupt and restart at any time.

Usage:
    python copier/copy_files.py [DEST]

    DEST defaults to output/local_copy if not supplied.

Example:
    python copier/copy_files.py D:\\local_drive_copy
"""

import sys
import json
import shutil
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

# Resolved relative to this file so the script works regardless of the working
# directory it is launched from.
OUTPUT_DIR   = Path(__file__).parent.parent / "output"
INDEX_FILE   = OUTPUT_DIR / "relevant_files.json"
DRIVE_ROOT   = Path("R:/")

# Default destination — override via command-line argument.
DEFAULT_DEST = OUTPUT_DIR / "local_copy"

# Tier 1 extensions: the pipeline extracts text from these formats via
# pdfplumber, python-docx, python-pptx, openpyxl, xlrd, and win32com.
# Set to None to copy ALL embeddable files (docs + text + code categories).
TIER1_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    """Convert a byte count to a human-readable string (e.g. 1536 -> '1.5 KB').

    Iterates through B/KB/MB/GB and returns as soon as the value drops below
    1024.  Falls through to TB for very large values.
    """
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_eta(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string.

    Examples:
        45   -> '45s'
        125  -> '2m 5s'
        3720 -> '1h 2m'
    """
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def relative_dest(src: Path, dest_root: Path) -> Path:
    """Compute the destination path for a source file under dest_root.

    Strips the R:\\ drive prefix from src and rebuilds the same relative
    folder structure under dest_root.  For example:

        src       = R:\\Electronics Design\\PCB\\board.pdf
        dest_root = D:\\local_copy
        result    = D:\\local_copy\\Electronics Design\\PCB\\board.pdf

    Falls back to stripping the first path component (the drive letter) if
    src is not under DRIVE_ROOT — this handles edge cases where the path was
    recorded with a different drive letter.
    """
    try:
        rel = src.relative_to(DRIVE_ROOT)
    except ValueError:
        # src is not under R:\ — strip the drive portion (parts[0]) manually.
        rel = Path(*src.parts[1:])
    return dest_root / rel


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """Entry point: parse args, load the index, and copy files.

    Flow:
        1. Resolve the destination root from argv[1] or DEFAULT_DEST.
        2. Load output/relevant_files.json produced by scanner/scan.py.
        3. Filter to the desired tier (TIER1_EXTS or all embeddable).
        4. Iterate over every target file:
             a. Print an in-place progress bar showing %, file counts,
                bytes transferred, and ETA derived from current throughput.
             b. Skip files already present at the destination with the
                correct size (idempotent / resumable).
             c. Create any missing parent directories.
             d. Copy the file with shutil.copy2 (preserves timestamps).
             e. Catch per-file OS errors so a single bad file does not
                abort the whole run; errors are collected and written to
                output/copy_errors.json at the end.
        5. Print a summary: files copied, skipped, and error count.
    """
    dest_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST

    # Abort early with a clear message if the scanner has not been run yet.
    if not INDEX_FILE.exists():
        print(f"ERROR: {INDEX_FILE} not found.")
        print("Run scanner/scan.py first to produce the file index.")
        sys.exit(1)

    with INDEX_FILE.open(encoding="utf-8") as f:
        records = json.load(f)

    # Filter records to the desired tier.
    # TIER1_EXTS = set  -> copy only the LLM-taggable document formats.
    # TIER1_EXTS = None -> copy every embeddable file (docs + text + code).
    if TIER1_EXTS is not None:
        targets = [r for r in records if r["ext"].lower() in TIER1_EXTS]
        label = "Tier 1 (taggable)"
    else:
        targets = records
        label = "all embeddable"

    total_files = len(targets)
    total_bytes = sum(r["size_bytes"] for r in targets)

    print(f"Source:      {DRIVE_ROOT}")
    print(f"Destination: {dest_root}")
    print(f"Files:       {total_files:,} {label} files  ({fmt_size(total_bytes)} total)")
    print()

    dest_root.mkdir(parents=True, exist_ok=True)

    copied     = 0   # files actually written this run
    skipped    = 0   # files skipped because dest already matched
    errors     = []  # list of {path, error} dicts for failed copies
    bytes_done = 0   # cumulative bytes transferred (used for ETA)
    start      = time.monotonic()

    for i, record in enumerate(targets, 1):
        src  = Path(record["path"])
        dest = relative_dest(src, dest_root)

        # Build and print an in-place progress bar.
        # Rate is bytes/second; ETA = remaining bytes / rate.
        elapsed = time.monotonic() - start
        rate    = bytes_done / elapsed if elapsed > 0 else 0
        eta_str = fmt_eta((total_bytes - bytes_done) / rate) if rate > 0 else "?"
        pct     = i / total_files * 100
        bar_w   = 20
        filled  = int(bar_w * i / total_files)
        bar     = "#" * filled + "-" * (bar_w - filled)
        print(
            f"\r[{bar}] {pct:5.1f}%  {i:,}/{total_files:,}  "
            f"{fmt_size(bytes_done)}/{fmt_size(total_bytes)}  "
            f"ETA {eta_str}  {src.name[:40]:<40}",
            end="", flush=True
        )

        # Idempotency check: skip if the destination file already exists and
        # has the same size as recorded in the index.  We use size rather than
        # mtime because network-drive timestamps can be unreliable.
        if dest.exists() and dest.stat().st_size == record["size_bytes"]:
            skipped    += 1
            bytes_done += record["size_bytes"]
            continue

        # Ensure the full directory chain exists before writing.
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            # copy2 copies file data AND metadata (timestamps, if supported).
            shutil.copy2(str(src), str(dest))
            copied     += 1
            bytes_done += record["size_bytes"]
        except (PermissionError, OSError, FileNotFoundError) as e:
            # Record the error but keep going — one bad file should not stop
            # the rest of the copy.
            errors.append({"path": str(src), "error": str(e)})

    elapsed = time.monotonic() - start
    print()  # newline after the last in-place progress bar
    print()
    print(f"Done in {fmt_eta(elapsed)}.")
    print(f"  Copied:  {copied:,} files")
    print(f"  Skipped: {skipped:,} files (already present, same size)")
    print(f"  Errors:  {len(errors)}")

    if errors:
        err_path = OUTPUT_DIR / "copy_errors.json"
        err_path.write_text(json.dumps(errors, indent=2), encoding="utf-8")
        print(f"  Error log -> {err_path}")


if __name__ == "__main__":
    main()
