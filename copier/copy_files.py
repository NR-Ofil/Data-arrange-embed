"""
Copy embeddable files from R:\ to a local folder, preserving relative paths.

Reads the list of embeddable files from output/relevant_files.json (produced
by scanner/scan.py) and copies each one into DEST_ROOT with the same folder
structure it has under R:\.

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

OUTPUT_DIR   = Path(__file__).parent.parent / "output"
INDEX_FILE   = OUTPUT_DIR / "relevant_files.json"
DRIVE_ROOT   = Path("R:/")

# Default destination — override via command-line argument.
DEFAULT_DEST = OUTPUT_DIR / "local_copy"

# Tier 1 extensions the pipeline actually extracts text from.
# Set to None to copy ALL embeddable files (docs + text + code).
TIER1_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def relative_dest(src: Path, dest_root: Path) -> Path:
    """Strip the drive letter / R:\ prefix and rebuild under dest_root."""
    try:
        rel = src.relative_to(DRIVE_ROOT)
    except ValueError:
        # Fallback: strip the drive portion manually
        rel = Path(*src.parts[1:])
    return dest_root / rel


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dest_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST

    # Load index
    if not INDEX_FILE.exists():
        print(f"ERROR: {INDEX_FILE} not found.")
        print("Run scanner/scan.py first to produce the file index.")
        sys.exit(1)

    with INDEX_FILE.open(encoding="utf-8") as f:
        records = json.load(f)

    # Filter to Tier 1 only (or all embeddable if TIER1_EXTS is None)
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

    copied = 0
    skipped = 0
    errors = []
    bytes_done = 0
    start = time.monotonic()

    for i, record in enumerate(targets, 1):
        src  = Path(record["path"])
        dest = relative_dest(src, dest_root)

        # Progress line (overwrite in place)
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

        # Skip if already copied and up to date
        if dest.exists() and dest.stat().st_size == record["size_bytes"]:
            skipped += 1
            bytes_done += record["size_bytes"]
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(str(src), str(dest))
            copied += 1
            bytes_done += record["size_bytes"]
        except (PermissionError, OSError, FileNotFoundError) as e:
            errors.append({"path": str(src), "error": str(e)})

    elapsed = time.monotonic() - start
    print()  # end the progress line
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
