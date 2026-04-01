"""Quick pipeline status check — run anytime while pipeline is running."""
import json
from pathlib import Path
from datetime import datetime

labels_file = Path("output/file_labels.json")
index_file  = Path("output/relevant_files.json")

if not labels_file.exists():
    print("No output/file_labels.json yet -- pipeline hasn't started.")
    exit()

labels  = json.loads(labels_file.read_text(encoding="utf-8"))
total   = len(json.loads(index_file.read_text(encoding="utf-8"))) if index_file.exists() else "?"

done      = len(labels)
skipped   = sum(1 for v in labels.values() if v.get("skipped"))
errors    = sum(1 for v in labels.values() if "error" in v)
tagged    = done - skipped - errors

mtime = datetime.fromtimestamp(labels_file.stat().st_mtime)

print(f"\n  Pipeline status  ({mtime.strftime('%Y-%m-%d %H:%M:%S')})")
print(f"  {'-'*40}")
print(f"  Total target files : {total:,}")
print(f"  Processed          : {done:,}")
print(f"  Tagged             : {tagged:,}")
print(f"  Skipped (no text)  : {skipped:,}")
print(f"  Errors             : {errors:,}")
if isinstance(total, int) and total:
    pct = done / total * 100
    bar = "#" * int(pct // 2) + "-" * (50 - int(pct // 2))
    print(f"\n  [{bar}] {pct:.1f}%\n")

recent = [(p, v) for p, v in labels.items() if not v.get("skipped") and "error" not in v][-5:]
if recent:
    print("  Last tagged:")
    for path, v in recent:
        print(f"    [{v.get('document_type','?'):<15}] {v.get('product',''):<15} {Path(path).name[:50]}")
print()
