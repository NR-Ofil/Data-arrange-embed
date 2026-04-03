"""
Phase 1 - Network Drive Scanner

Walks R:\\ (or a given subdirectory) and produces a complete file index
that later phases use for tagging and embedding.

Usage:
    python scanner/scan.py              # full scan of R:\\
    python scanner/scan.py R:\\Subdir   # test on a specific folder

Outputs (written to output/):
    file_index.json       - full metadata for every file on the drive
    relevant_files.json   - embeddable subset (documents, text, code)
    summary_by_type.csv   - per-extension file counts and total sizes
    report.html           - visual scan report (open in browser)
    scan_errors.json      - files that could not be read (permissions etc.)
"""

import os
import json
import csv
import sys
from pathlib import Path
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────

DRIVE_ROOT = Path("R:/")
OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Extensions considered relevant for LLM embedding, grouped by category.
# Each category maps to a set of lowercase extensions.
EMBEDDABLE = {
    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                 ".odt", ".ods", ".odp", ".rtf"},
    "text":     {".txt", ".md", ".rst", ".csv", ".log", ".msg", ".eml"},
    "code":     {".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".cs",
                 ".go", ".rs", ".sh", ".bat", ".ps1", ".sql", ".html",
                 ".css", ".json", ".yaml", ".yml", ".xml", ".toml", ".ini",
                 ".cfg", ".env"},
}

# Extensions explicitly skipped — binary/media formats with no embeddable text.
SKIP_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".sys", ".msi", ".bin", ".iso",
    ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".svg", ".webp", ".raw", ".psd", ".ai", ".eps",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".db", ".sqlite", ".mdb", ".accdb",
    ".pst", ".ost",
    ".tmp", ".bak", ".swp", ".DS_Store", ".lnk",
}

# Folder names pruned from the walk entirely — their contents are never visited.
SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", ".git", "__pycache__",
    "node_modules", ".vs", ".idea", "Thumbs.db",
}

# Files larger than this threshold are still indexed but flagged as large_file=True.
# The embedder will chunk them carefully.
EMBED_SIZE_WARN_MB = 50


# ── Helpers ──────────────────────────────────────────────────────────────────

def classify_extension(ext: str) -> str:
    """Return the category for a file extension.

    Returns one of: 'document', 'text', 'code', 'skip', or 'other'.
    'other' covers extensions not in any list (e.g. .sldprt CAD files).
    """
    ext = ext.lower()
    for category, exts in EMBEDDABLE.items():
        if ext in exts:
            return category
    if ext in SKIP_EXTENSIONS:
        return "skip"
    return "other"


def fmt_size(n: int) -> str:
    """Convert byte count to a human-readable string (e.g. '1.4 GB')."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Main scan ────────────────────────────────────────────────────────────────

def scan(root: Path) -> tuple[list[dict], dict]:
    """Walk the directory tree under root and collect metadata for every file.

    Skips directories listed in SKIP_DIRS and any hidden directories (name
    starting with '.').  Permission errors are caught per-file so the walk
    continues even if individual files are inaccessible.

    Returns:
        records: list of dicts, one per file, with keys:
                 path, name, ext, category, size_bytes, size_human,
                 modified, embeddable, large_file
        stats:   dict mapping extension → {count, total_bytes, category}
    """
    records = []
    errors = []
    stats: dict[str, dict] = {}  # ext -> {count, total_bytes, category}

    total_seen = 0
    start = datetime.now()

    print(f"Scanning {root} …  (Ctrl-C to abort)")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune skip dirs in-place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            total_seen += 1

            if total_seen % 1000 == 0:
                elapsed = (datetime.now() - start).seconds
                print(f"  {total_seen:,} files scanned … ({elapsed}s)", end="\r")

            try:
                stat = fpath.stat()
            except (PermissionError, OSError) as e:
                errors.append({"path": str(fpath), "error": str(e)})
                continue

            ext = fpath.suffix.lower()
            category = classify_extension(ext)
            size = stat.st_size

            # Some files on R:\ have corrupt timestamps — guard against crashes.
            try:
                modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
            except (OSError, OverflowError, ValueError):
                modified = None

            record = {
                "path":       str(fpath),
                "name":       fname,
                "ext":        ext,
                "category":   category,
                "size_bytes": size,
                "size_human": fmt_size(size),
                "modified":   modified,
                "embeddable": category in EMBEDDABLE,
                "large_file": size > EMBED_SIZE_WARN_MB * 1024 * 1024,
            }
            records.append(record)

            # Accumulate per-extension stats for the CSV summary.
            if ext not in stats:
                stats[ext] = {"count": 0, "total_bytes": 0, "category": category}
            stats[ext]["count"] += 1
            stats[ext]["total_bytes"] += size

    elapsed = (datetime.now() - start).seconds
    print(f"\nDone. {total_seen:,} files in {elapsed}s. {len(errors)} errors.")
    if errors:
        err_path = OUTPUT_DIR / "scan_errors.json"
        err_path.write_text(json.dumps(errors, indent=2), encoding="utf-8")
        print(f"  Errors written to {err_path}")

    return records, stats


# ── Output writers ────────────────────────────────────────────────────────────

def write_html_report(records: list[dict], stats: dict, root: Path):
    """Produce a self-contained HTML report you can open in any browser.

    Includes: category storage breakdown, top extensions by count,
    top folders by embeddable file count, 50 largest embeddable files.
    """

    # Aggregate counts and bytes per category.
    by_cat: dict[str, dict] = {}
    for r in records:
        c = r["category"]
        if c not in by_cat:
            by_cat[c] = {"count": 0, "bytes": 0}
        by_cat[c]["count"] += 1
        by_cat[c]["bytes"] += r["size_bytes"]

    total_files = len(records)
    total_bytes = sum(r["size_bytes"] for r in records)
    embeddable  = [r for r in records if r["embeddable"]]
    large_files = [r for r in embeddable if r["large_file"]]

    # Top 20 extensions by file count.
    top_ext = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)[:20]

    # Top 50 largest embeddable files (to identify chunking candidates).
    top_large = sorted(embeddable, key=lambda x: x["size_bytes"], reverse=True)[:50]

    # Top 30 first-level folders by embeddable file count.
    folder_counts: dict[str, int] = {}
    for r in embeddable:
        parts = Path(r["path"]).parts
        folder = parts[1] if len(parts) > 1 else "(root)"
        folder_counts[folder] = folder_counts.get(folder, 0) + 1
    top_folders = sorted(folder_counts.items(), key=lambda x: x[1], reverse=True)[:30]

    CAT_COLOR = {
        "document": "#4A90D9", "text": "#27AE60", "code": "#8E44AD",
        "skip": "#BDC3C7", "other": "#E67E22",
    }

    def color(cat):
        return CAT_COLOR.get(cat, "#95A5A6")

    def ext_rows():
        rows = []
        for ext, d in top_ext:
            rows.append(f"""
            <tr>
              <td><code>{ext or "(none)"}</code></td>
              <td><span class="badge" style="background:{color(d['category'])}">{d['category']}</span></td>
              <td>{d['count']:,}</td>
              <td>{fmt_size(d['total_bytes'])}</td>
            </tr>""")
        return "".join(rows)

    def folder_rows():
        rows = []
        for folder, cnt in top_folders:
            rows.append(f"<tr><td>{folder}</td><td>{cnt:,}</td></tr>")
        return "".join(rows)

    def large_rows():
        rows = []
        for r in top_large:
            flag = " (large)" if r["large_file"] else ""
            rows.append(f"""
            <tr>
              <td style="word-break:break-all;font-size:12px">{r['path']}</td>
              <td><span class="badge" style="background:{color(r['category'])}">{r['category']}</span></td>
              <td>{r['size_human']}{flag}</td>
              <td>{r['modified'][:10]}</td>
            </tr>""")
        return "".join(rows)

    def cat_bars():
        bars = []
        for cat, d in sorted(by_cat.items(), key=lambda x: x[1]["bytes"], reverse=True):
            pct = d["bytes"] / total_bytes * 100 if total_bytes else 0
            bars.append(f"""
            <div class="bar-row">
              <div class="bar-label">{cat}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width:{pct:.1f}%;background:{color(cat)}"></div>
              </div>
              <div class="bar-info">{d['count']:,} files &nbsp;·&nbsp; {fmt_size(d['bytes'])}</div>
            </div>""")
        return "".join(bars)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Drive Scan Report — {root}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 0; background: #f5f6fa; color: #2c3e50; }}
  .header {{ background: #2c3e50; color: white; padding: 24px 40px; }}
  .header h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .header p  {{ margin: 0; opacity: .7; font-size: 14px; }}
  .content {{ padding: 32px 40px; max-width: 1200px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .card {{ background: white; border-radius: 8px; padding: 20px 24px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 160px; }}
  .card .val {{ font-size: 28px; font-weight: 700; color: #2c3e50; }}
  .card .lbl {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; }}
  .section {{ background: white; border-radius: 8px; padding: 24px;
              box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 24px; }}
  .section h2 {{ margin: 0 0 16px; font-size: 16px; border-bottom: 1px solid #ecf0f1;
                 padding-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 10px; background: #f8f9fa;
        border-bottom: 2px solid #ecf0f1; color: #7f8c8d; font-weight: 600; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #f0f0f0; }}
  tr:hover td {{ background: #fafbfc; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
            color: white; font-size: 11px; font-weight: 600; }}
  .bar-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
  .bar-label {{ width: 90px; font-size: 13px; text-align: right; color: #555; }}
  .bar-track {{ flex: 1; height: 18px; background: #ecf0f1; border-radius: 4px; overflow: hidden; }}
  .bar-fill  {{ height: 100%; border-radius: 4px; transition: width .3s; }}
  .bar-info  {{ font-size: 12px; color: #7f8c8d; white-space: nowrap; }}
  .warn {{ color: #e67e22; font-weight: 600; }}
</style>
</head>
<body>
<div class="header">
  <h1>Drive Scan Report</h1>
  <p>{root} &nbsp;·&nbsp; scanned {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
</div>
<div class="content">

  <div class="cards">
    <div class="card"><div class="val">{total_files:,}</div><div class="lbl">Total files</div></div>
    <div class="card"><div class="val">{fmt_size(total_bytes)}</div><div class="lbl">Total size</div></div>
    <div class="card"><div class="val" style="color:#27AE60">{len(embeddable):,}</div><div class="lbl">Embeddable files</div></div>
    <div class="card"><div class="val" style="color:#27AE60">{fmt_size(sum(r["size_bytes"] for r in embeddable))}</div><div class="lbl">Embeddable size</div></div>
    {"<div class='card'><div class='val warn'>" + str(len(large_files)) + "</div><div class='lbl'>Large files (&gt;50MB)</div></div>" if large_files else ""}
  </div>

  <div class="section">
    <h2>Storage by category</h2>
    {cat_bars()}
  </div>

  <div class="section">
    <h2>Top 20 file types by count</h2>
    <table>
      <tr><th>Extension</th><th>Category</th><th>Files</th><th>Total size</th></tr>
      {ext_rows()}
    </table>
  </div>

  <div class="section">
    <h2>Top 30 folders by embeddable file count</h2>
    <table>
      <tr><th>Folder (top-level)</th><th>Embeddable files</th></tr>
      {folder_rows()}
    </table>
  </div>

  <div class="section">
    <h2>50 largest embeddable files</h2>
    <table>
      <tr><th>Path</th><th>Category</th><th>Size</th><th>Modified</th></tr>
      {large_rows()}
    </table>
  </div>

</div>
</body>
</html>"""

    path = OUTPUT_DIR / "report.html"
    path.write_text(html, encoding="utf-8")
    print(f"report.html            -> {path}  (open in browser)")


def write_index(records: list[dict]):
    """Write all file metadata to output/file_index.json.

    This is the full index used by the search portal (portal/index.html).
    """
    path = OUTPUT_DIR / "file_index.json"
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"file_index.json        -> {path}  ({len(records):,} files)")


def write_summary(stats: dict):
    """Write per-extension counts and sizes to output/summary_by_type.csv.

    Sorted by total bytes descending so the most space-consuming types appear first.
    """
    path = OUTPUT_DIR / "summary_by_type.csv"
    rows = sorted(stats.items(), key=lambda x: x[1]["total_bytes"], reverse=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["extension", "category", "count", "total_bytes", "total_size"])
        for ext, d in rows:
            w.writerow([ext or "(no ext)", d["category"], d["count"],
                        d["total_bytes"], fmt_size(d["total_bytes"])])
    print(f"summary_by_type.csv    -> {path}  ({len(rows)} extension types)")


def write_relevant(records: list[dict]):
    """Write the embeddable subset to output/relevant_files.json.

    Only files with embeddable=True are included. This is the input file
    consumed by embedder/pipeline.py in Phase 2+3.
    """
    relevant = [r for r in records if r["embeddable"]]
    path = OUTPUT_DIR / "relevant_files.json"
    path.write_text(json.dumps(relevant, indent=2), encoding="utf-8")
    total_bytes = sum(r["size_bytes"] for r in relevant)
    large = [r for r in relevant if r["large_file"]]
    print(f"relevant_files.json    -> {path}  ({len(relevant):,} files, "
          f"{fmt_size(total_bytes)} total)")
    if large:
        print(f"  WARNING: {len(large)} files exceed {EMBED_SIZE_WARN_MB}MB — "
              f"will need careful chunking in Phase 3")


def print_top_summary(records: list[dict]):
    """Print a category breakdown table to stdout after the scan completes."""
    by_cat: dict[str, int] = {}
    by_cat_bytes: dict[str, int] = {}
    for r in records:
        c = r["category"]
        by_cat[c] = by_cat.get(c, 0) + 1
        by_cat_bytes[c] = by_cat_bytes.get(c, 0) + r["size_bytes"]

    print("\n-- Category breakdown ------------------------------------------------")
    for cat in sorted(by_cat, key=lambda x: by_cat_bytes[x], reverse=True):
        print(f"  {cat:<12} {by_cat[cat]:>8,} files   {fmt_size(by_cat_bytes[cat]):>10}")
    print("----------------------------------------------------------------------")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DRIVE_ROOT

    if not root.exists():
        print(f"ERROR: {root} is not accessible. Is the drive mapped?")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    records, stats = scan(root)

    print("\nWriting output files ...")
    write_index(records)
    write_summary(stats)
    write_relevant(records)
    write_html_report(records, stats, root)
    print_top_summary(records)
