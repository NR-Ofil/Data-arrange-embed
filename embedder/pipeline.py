"""
Phase 2+3 Pipeline — Tag and embed documents/presentations

Before running:
  1. Set OLLAMA_BASE_URL below to your Jetson's Tailscale IP
  2. pip install -r embedder/requirements.txt
  3. python embedder/pipeline.py             # tag + embed
     python embedder/pipeline.py --tag-only  # tagging only, no ChromaDB

Outputs:
  output/file_labels.json   tags, summary, keywords per file (incremental)
  chroma_db/                vector store for semantic search
  output/pipeline_errors.json
"""

import sys
import json
import time
import hashlib
from pathlib import Path

# Allow importing extract.py from the same directory
sys.path.insert(0, str(Path(__file__).parent))

# ── Configuration ─────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = "http://<jetson-tailscale-ip>:11434"   # ← SET THIS
LLM_MODEL       = "llama3.2"
EMBED_MODEL     = "nomic-embed-text"

CHUNK_WORDS     = 400    # words per embedding chunk
CHUNK_OVERLAP   = 50     # word overlap between chunks

# Only these extensions are processed (document + presentation category)
TARGET_EXTENSIONS = {
    ".pdf", ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt", ".ods", ".odp",
}

# ── Paths ─────────────────────────────────────────────────────────────────────

OUTPUT_DIR  = Path(__file__).parent.parent / "output"
CHROMA_DIR  = Path(__file__).parent.parent / "chroma_db"
LABELS_FILE = OUTPUT_DIR / "file_labels.json"
INDEX_FILE  = OUTPUT_DIR / "relevant_files.json"

# ── Ollama ────────────────────────────────────────────────────────────────────

import requests

TAG_PROMPT = """\
You are analyzing a file from an engineering R&D company that builds optical and sensing instruments \
(UV cameras, photon counters, image intensifiers, FPGA-based systems, gas detectors).

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
when looking for this document."""


def ollama_tag(tag_text: str, filename: str) -> dict:
    prompt = TAG_PROMPT.format(filename=filename, text=tag_text)
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": LLM_MODEL, "prompt": prompt, "format": "json", "stream": False},
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "{}")
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "bad JSON from LLM"}
    except Exception as e:
        return {"error": str(e)}


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
        print(f"Jetson OK. Models available: {', '.join(models) or 'none'}")
        missing = []
        if not any(LLM_MODEL in m for m in models):
            missing.append(LLM_MODEL)
        if not any(EMBED_MODEL in m for m in models):
            missing.append(EMBED_MODEL)
        if missing:
            print(f"WARNING: missing models — run on Jetson: ollama pull {' && ollama pull '.join(missing)}")
        return True
    except Exception as e:
        print(f"ERROR: Cannot reach Jetson at {OLLAMA_BASE_URL}")
        print(f"  {e}")
        print("  Is Tailscale connected? Is Ollama running with OLLAMA_HOST=0.0.0.0?")
        return False


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += CHUNK_WORDS - CHUNK_OVERLAP
    return chunks


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def get_collection():
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(
        name="drive_documents",
        metadata={"hnsw:space": "cosine"},
    )


def embed_file(collection, record: dict, full_text: str, label: dict) -> int:
    chunks = chunk_text(full_text)
    if not chunks:
        return 0

    path_hash = hashlib.md5(record["path"].encode()).hexdigest()[:10]
    tags_str = ",".join(label.get("tags", []))

    ids, embeddings, documents, metadatas = [], [], [], []

    for i, chunk in enumerate(chunks):
        vec = ollama_embed(chunk)
        if vec is None:
            continue
        ids.append(f"{path_hash}_{i}")
        embeddings.append(vec)
        documents.append(chunk)
        metadatas.append({
            "path":          record["path"],
            "name":          record["name"],
            "ext":           record["ext"],
            "document_type": str(label.get("document_type", "")),
            "product":       str(label.get("product", "")),
            "domain":        str(label.get("domain", "")),
            "tags":          tags_str,
            "chunk":         i,
            "total_chunks":  len(chunks),
        })

    if ids:
        collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
    return len(ids)


# ── Labels I/O ────────────────────────────────────────────────────────────────

def load_labels() -> dict:
    if LABELS_FILE.exists():
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_labels(labels: dict):
    LABELS_FILE.write_text(
        json.dumps(labels, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main(tag_only: bool = False):
    print("─" * 65)
    print("Pipeline: Tag + Embed  (documents and presentations)")
    print("─" * 65)

    if "<jetson-tailscale-ip>" in OLLAMA_BASE_URL:
        print("ERROR: Set OLLAMA_BASE_URL at the top of pipeline.py first.")
        sys.exit(1)

    if not check_ollama():
        sys.exit(1)

    if not INDEX_FILE.exists():
        print("ERROR: output/relevant_files.json not found. Run scanner first.")
        sys.exit(1)

    all_relevant = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    targets = [f for f in all_relevant if f["ext"].lower() in TARGET_EXTENSIONS]

    print(f"\nTarget files (docs + presentations):  {len(targets):,}")

    labels  = load_labels()
    done    = set(labels.keys())
    todo    = [f for f in targets if f["path"] not in done]

    print(f"Already processed:                    {len(done):,}")
    print(f"Remaining:                            {len(todo):,}")

    if not todo:
        print("\nAll files already processed.")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    collection = None
    if not tag_only:
        CHROMA_DIR.mkdir(exist_ok=True)
        collection = get_collection()

    from extract import extract

    errors = []
    t0 = time.time()

    for i, record in enumerate(todo, 1):
        path = Path(record["path"])

        # ETA
        elapsed = time.time() - t0
        avg     = elapsed / i
        eta_s   = avg * (len(todo) - i)
        eta     = f"{eta_s/3600:.1f}h" if eta_s > 3600 else f"{eta_s/60:.0f}m"

        print(f"[{i:>6,}/{len(todo):,}]  ETA {eta:>5}  {path.name[:55]}", end="  ", flush=True)

        # 1 — Extract
        tag_text, full_text = extract(path)
        if not tag_text.strip():
            print("(no text — skipped)")
            labels[record["path"]] = {"skipped": True, "reason": "no text extracted",
                                      "path": record["path"], "name": record["name"]}
            save_labels(labels)
            continue

        # 2 — Tag
        label = ollama_tag(tag_text, path.name)
        if "error" in label:
            print(f"TAG ERR: {label['error']}")
            errors.append({"path": record["path"], "stage": "tag", "error": label["error"]})
            continue

        # Attach file metadata to label record
        label.update({
            "path":       record["path"],
            "name":       record["name"],
            "ext":        record["ext"],
            "size_human": record.get("size_human", ""),
            "modified":   record.get("modified"),
        })

        # 3 — Embed
        chunks_stored = 0
        if not tag_only and full_text.strip():
            try:
                chunks_stored = embed_file(collection, record, full_text, label)
            except Exception as e:
                errors.append({"path": record["path"], "stage": "embed", "error": str(e)})

        label["chunks_embedded"] = chunks_stored
        labels[record["path"]] = label
        save_labels(labels)

        doc_type = label.get("document_type", "?")
        product  = label.get("product", "")
        print(f"[{doc_type}]  {product}  ({chunks_stored} chunks)")

    elapsed_total = time.time() - t0
    print(f"\nFinished in {elapsed_total/3600:.1f}h. "
          f"{len(todo):,} files processed. {len(errors)} errors.")

    if errors:
        err_path = OUTPUT_DIR / "pipeline_errors.json"
        err_path.write_text(json.dumps(errors, indent=2))
        print(f"Errors → {err_path}")


if __name__ == "__main__":
    tag_only = "--tag-only" in sys.argv
    if tag_only:
        print("Mode: tag only (skipping embedding)")
    main(tag_only=tag_only)
