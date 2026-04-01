"""
Phase 2+3 Pipeline — Tag and embed documents/presentations

Before running:
  1. On Jetson: ollama pull nomic-embed-text
  2. On Jetson: expose Ollama remotely (see CLAUDE.md)
  3. pip install -r embedder/requirements.txt
  4. python embedder/pipeline.py             # tag + embed
     python embedder/pipeline.py --tag-only  # tagging only, no Qdrant

Outputs:
  output/file_labels.json    tags, summary, keywords per file (incremental — safe to interrupt)
  output/pipeline_errors.json
  Qdrant collection: drive_documents  (on Jetson at QDRANT_URL)
"""

import sys
import io
import json
import time
import hashlib

# Force UTF-8 output on Windows — prevents crashes on Hebrew/Greek/special chars in filenames
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Configuration ─────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = "http://100.84.73.5:11434"
QDRANT_URL      = "http://100.84.73.5:6333"
QDRANT_COLLECTION = "drive_documents"

LLM_MODEL   = "qwen3:8b"
EMBED_MODEL = "nomic-embed-text"

CHUNK_WORDS   = 400
CHUNK_OVERLAP = 50

TARGET_EXTENSIONS = {
    ".pdf", ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt", ".ods", ".odp",
}

# ── Paths ─────────────────────────────────────────────────────────────────────

OUTPUT_DIR  = Path(__file__).parent.parent / "output"
LABELS_FILE = OUTPUT_DIR / "file_labels.json"
INDEX_FILE  = OUTPUT_DIR / "relevant_files.json"

# ── Ollama ────────────────────────────────────────────────────────────────────

import requests

TAG_PROMPT = """\
You are analyzing a file from an engineering R&D company that builds optical and sensing instruments \
(UV cameras, photon counters, image intensifiers, FPGA-based systems, gas detectors, oil spill detectors).
Product lines include: Mirage, DC-10, Daycor, i-COR, SuperbHD, UvolleHD, Luminar, Scalar, Superb.

File name: {filename}
File content (first ~1000 words):
{text}

Return a JSON object with these exact fields:
{{
  "summary": "2 concise sentences describing what this document is about and its purpose",
  "document_type": "one of: datasheet, specification, design_review, test_report, cost_sheet, BOM, \
meeting_notes, presentation, manual, proposal, research_paper, experiment, drawing, other",
  "product": "the product or project name (e.g. Mirage, DC-10, Daycor, i-COR, SuperbHD, Luminar, \
Scalar, FPGA_2025) or 'general' if not product-specific",
  "domain": "primary technical domain: optics, electronics, FPGA, mechanics, chemistry, software, \
imaging, detection, management, other",
  "tags": ["8 to 15 specific tags describing content, topics, and themes"],
  "keywords": ["10 to 20 important technical terms, part numbers, component names, people, \
standards, or concepts that appear in this document"]
}}

Be thorough. Tags and keywords are used for search — include anything a colleague might search for \
when looking for this document. Do not include any text outside the JSON object."""


def ollama_tag(tag_text: str, filename: str) -> dict:
    prompt = TAG_PROMPT.format(filename=filename, text=tag_text)
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt, "format": "json", "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "{}")
            # qwen3 sometimes wraps output in <think>...</think> before the JSON
            if "<think>" in raw:
                raw = raw[raw.rfind("</think>") + len("</think>"):].strip()
            # find the JSON object in case there's surrounding text
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt == 2:
                return {"error": "bad JSON from LLM after 3 attempts"}
            time.sleep(2)
        except Exception as e:
            return {"error": str(e)}
    return {"error": "failed after 3 attempts"}


def ollama_embed(text: str) -> list | None:
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except Exception:
        return None


def check_ollama() -> bool:
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        models = [m["name"] for m in resp.json().get("models", [])]
        print(f"Ollama OK. Models: {', '.join(models) or 'none'}")
        missing = []
        if not any(LLM_MODEL in m for m in models):
            missing.append(LLM_MODEL)
        if not any(EMBED_MODEL in m for m in models):
            missing.append(EMBED_MODEL)
        if missing:
            print(f"\nWARNING: missing models. On Jetson run:")
            for m in missing:
                print(f"  ollama pull {m}")
            return False
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach Ollama at {OLLAMA_BASE_URL}")
        print(f"  {e}")
        print("\n  Fix on Jetson:")
        print("    sudo mkdir -p /etc/systemd/system/ollama.service.d")
        print('    echo -e "[Service]\\nEnvironment=\\"OLLAMA_HOST=0.0.0.0\\"" | sudo tee /etc/systemd/system/ollama.service.d/override.conf')
        print("    sudo systemctl daemon-reload && sudo systemctl restart ollama")
        return False


# ── Qdrant ────────────────────────────────────────────────────────────────────

def get_qdrant_collection(vector_size: int):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=QDRANT_URL, timeout=30)

    existing = [c.name for c in client.get_collections().collections]
    if QDRANT_COLLECTION not in existing:
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"Created Qdrant collection: {QDRANT_COLLECTION}  (vector size: {vector_size})")
    else:
        print(f"Using existing Qdrant collection: {QDRANT_COLLECTION}")

    return client


def embed_file(client, record: dict, full_text: str, label: dict) -> int:
    from qdrant_client.models import PointStruct

    chunks = chunk_text(full_text)
    if not chunks:
        return 0

    path_hash = int(hashlib.md5(record["path"].encode()).hexdigest(), 16)
    tags_str = ",".join(label.get("tags", []))
    points = []

    for i, chunk in enumerate(chunks):
        vec = ollama_embed(chunk)
        if vec is None:
            continue
        # deterministic integer ID: hash of path + chunk index
        point_id = (path_hash + i) % (2**63)
        points.append(PointStruct(
            id=point_id,
            vector=vec,
            payload={
                "path":          record["path"],
                "name":          record["name"],
                "ext":           record["ext"],
                "document_type": str(label.get("document_type", "")),
                "product":       str(label.get("product", "")),
                "domain":        str(label.get("domain", "")),
                "tags":          tags_str,
                "summary":       str(label.get("summary", "")),
                "text":          chunk,
                "chunk":         i,
            },
        ))

    if points:
        client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    return len(points)


def check_qdrant() -> bool:
    try:
        resp = requests.get(f"{QDRANT_URL}/healthz", timeout=10)
        if resp.status_code == 200:
            print(f"Qdrant OK at {QDRANT_URL}")
            return True
        return False
    except Exception as e:
        print(f"ERROR: Cannot reach Qdrant at {QDRANT_URL}: {e}")
        return False


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks, start = [], 0
    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += CHUNK_WORDS - CHUNK_OVERLAP
    return chunks


# ── Progress display ──────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds//60}m {seconds%60:02d}s"
    return f"{seconds//3600}h {(seconds%3600)//60:02d}m"


class Progress:
    BAR_WIDTH = 30

    def __init__(self, total: int):
        self.total    = total
        self.done     = 0
        self.skipped  = 0
        self.errors   = 0
        self.t0       = time.time()
        self._stage   = ""
        self._file    = ""

    def update(self, file: str, stage: str):
        self._file  = file
        self._stage = stage
        self._draw()

    def complete(self, skipped=False, error=False):
        self.done += 1
        if skipped:
            self.skipped += 1
        if error:
            self.errors += 1
        self._draw()

    def _draw(self):
        elapsed   = time.time() - self.t0
        pct       = self.done / self.total if self.total else 0
        filled    = int(self.BAR_WIDTH * pct)
        bar       = "#" * filled + "-" * (self.BAR_WIDTH - filled)
        rate      = self.done / elapsed * 3600 if elapsed > 1 else 0
        remaining = self.total - self.done
        eta_s     = (remaining / (self.done / elapsed)) if self.done > 0 else 0

        name = self._file[:45].ljust(45)
        stage = f"[{self._stage}]".ljust(12)

        line = (
            f"\r  {bar}  {pct*100:5.1f}%  "
            f"{self.done:,}/{self.total:,}  "
            f"elapsed {fmt_duration(elapsed)}  "
            f"ETA {fmt_duration(eta_s)}  "
            f"{rate:,.0f} files/h  "
            f"err {self.errors}  "
            f"{stage} {name}"
        )
        print(line, end="", flush=True)

    def finish(self):
        elapsed = time.time() - self.t0
        print(f"\n\n  Done in {fmt_duration(elapsed)}.  "
              f"{self.done:,} processed  |  "
              f"{self.skipped:,} skipped (no text)  |  "
              f"{self.errors:,} errors")


# ── Labels I/O ────────────────────────────────────────────────────────────────

def load_labels() -> dict:
    if LABELS_FILE.exists():
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_labels(labels: dict):
    LABELS_FILE.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(tag_only: bool = False):
    print("-" * 65)
    print("Pipeline: Tag + Embed  (documents and presentations)")
    print(f"Ollama:   {OLLAMA_BASE_URL}  model={LLM_MODEL}")
    print(f"Qdrant:   {QDRANT_URL}  collection={QDRANT_COLLECTION}")
    print("-" * 65)

    if not check_ollama():
        sys.exit(1)

    if not tag_only and not check_qdrant():
        sys.exit(1)

    if not INDEX_FILE.exists():
        print("ERROR: output/relevant_files.json not found. Run scanner first.")
        sys.exit(1)

    all_relevant = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    targets = [f for f in all_relevant if f["ext"].lower() in TARGET_EXTENSIONS]
    print(f"\nTarget files (docs + presentations): {len(targets):,}")

    labels = load_labels()
    done   = set(labels.keys())
    todo   = [f for f in targets if f["path"] not in done]

    print(f"Already processed:                   {len(done):,}")
    print(f"Remaining:                            {len(todo):,}")

    if not todo:
        print("\nAll files already processed.")
        return

    # Detect embedding vector size from a test embedding
    qdrant_client = None
    if not tag_only:
        print("\nDetecting embedding vector size…")
        test_vec = ollama_embed("test")
        if test_vec is None:
            print("ERROR: Could not get embedding from Ollama. Is nomic-embed-text pulled?")
            sys.exit(1)
        vector_size = len(test_vec)
        print(f"Vector size: {vector_size}")
        qdrant_client = get_qdrant_collection(vector_size)

    from extract import extract

    errors = []
    OUTPUT_DIR.mkdir(exist_ok=True)
    progress = Progress(len(todo))

    print()  # blank line before progress bar

    for record in todo:
        path = Path(record["path"])
        progress.update(path.name, "extract")

        # 1 — Extract text
        tag_text, full_text = extract(path)
        if not tag_text.strip():
            labels[record["path"]] = {"skipped": True, "reason": "no text extracted",
                                       "path": record["path"], "name": record["name"]}
            save_labels(labels)
            progress.complete(skipped=True)
            continue

        # 2 — Tag via LLM
        progress.update(path.name, "tagging")
        label = ollama_tag(tag_text, path.name)
        if "error" in label:
            errors.append({"path": record["path"], "stage": "tag", "error": label["error"]})
            save_labels(labels)
            progress.complete(error=True)
            continue

        label.update({
            "path":       record["path"],
            "name":       record["name"],
            "ext":        record["ext"],
            "size_human": record.get("size_human", ""),
            "modified":   record.get("modified"),
        })

        # 3 — Embed into Qdrant
        chunks_stored = 0
        if not tag_only and full_text.strip():
            progress.update(path.name, "embedding")
            try:
                chunks_stored = embed_file(qdrant_client, record, full_text, label)
            except Exception as e:
                errors.append({"path": record["path"], "stage": "embed", "error": str(e)})
                progress.complete(error=True)

        label["chunks_embedded"] = chunks_stored
        labels[record["path"]] = label
        save_labels(labels)
        progress.complete()

    progress.finish()

    if errors:
        err_path = OUTPUT_DIR / "pipeline_errors.json"
        err_path.write_text(json.dumps(errors, indent=2))
        print(f"  Errors → {err_path}")


if __name__ == "__main__":
    tag_only = "--tag-only" in sys.argv
    if tag_only:
        print("Mode: tag only (skipping Qdrant embedding)")
    main(tag_only=tag_only)
