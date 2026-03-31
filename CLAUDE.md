# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## Project Goal

Three-phase pipeline to make a large network drive (mapped as `R:\`, ~800GB, ~927K files) queryable via a local LLM:

1. **Phase 1 — Scan** (`scanner/`): Walk the drive, classify every file by type, produce JSON/CSV indexes.
2. **Phase 2+3 — Tag & Embed** (`embedder/`): Extract text from documents/presentations, tag via LLM, embed into ChromaDB.
3. **Portal** (`portal/`): Local web search interface over the indexed drive.

### Drive profile (from scan)
- 52,730 PDFs (22.4 GB), ~14K Word docs, ~14K Excel sheets, presentations — these are the embedding targets
- Large `other` category: `.sldprt`/`.sldasm` (SolidWorks CAD), `.cdb`/`.hdb` (EDA databases) — binary, not embeddable
- Top folders: `FPGA_2025`, `Electronics Design`, `DC-10`, `Mirage`, `Daycor`, `i-COR`, `CTO`, `Chemistry Lab`
- This is an engineering R&D company building optical/sensing instruments (UV cameras, photon counters, FPGA systems)

## Infrastructure

- **LLM server**: Jetson Orin NX 16GB running Ollama, reachable over Tailscale.
  - LLM model: `llama3.2`
  - Embedding model: `nomic-embed-text`
  - Ollama API base: `http://<jetson-tailscale-ip>:11434`
- **Vector store**: ChromaDB, persisted locally under `chroma_db/` (gitignored).
- **Drive**: `R:\` mapped on Windows; scripts accept a path override as `argv[1]`.

## Running the Search Portal

```bash
python portal/serve.py
```

Opens `http://localhost:8765/portal/index.html` in the browser automatically. Requires `output/file_index.json` to exist (run Phase 1 first). No third-party dependencies — stdlib only.

## Running Phase 1

```bash
python scanner/scan.py           # scans R:\
python scanner/scan.py R:\Subdir  # scan a subfolder to test
```

Outputs (all gitignored under `output/`):

| File | Contents |
|---|---|
| `output/file_index.json` | Every file: path, ext, category, size, modified |
| `output/summary_by_type.csv` | Per-extension counts and sizes, sorted by size |
| `output/relevant_files.json` | Embeddable subset (docs, text, code only) |
| `output/scan_errors.json` | Permission-denied or unreadable paths |

## File Classification

Defined at the top of [scanner/scan.py](scanner/scan.py):

- `EMBEDDABLE` — three categories: `document`, `text`, `code`. These are the only files passed to Phase 3.
- `SKIP_EXTENSIONS` — binary/media/archive formats explicitly ignored.
- `SKIP_DIRS` — folder names pruned from the walk entirely.
- `EMBED_SIZE_WARN_MB = 50` — files above this are flagged `large_file: true` in the index.

## Known Edge Cases

- **Corrupt timestamps**: some files on `R:\` have invalid `st_mtime` values that crash `datetime.fromtimestamp()`. Always wrap timestamp conversion in `try/except (OSError, OverflowError, ValueError)` and fall back to `None`.
- **Docstrings with Windows paths**: use `R:\\` (escaped) or raw strings in docstrings — `R:\` triggers a `SyntaxWarning` for invalid escape sequence.
- **Virtual environment**: `.venv/` is at the project root. Run scripts with `.venv/Scripts/python.exe` or activate first with `.venv\Scripts\activate`.

## Running Phase 2+3 (Tag + Embed)

```bash
pip install -r embedder/requirements.txt

# Tag only (no Jetson embedding) — useful to test LLM tagging first
python embedder/pipeline.py --tag-only

# Full pipeline: tag + embed into ChromaDB
python embedder/pipeline.py
```

**Before running**: set `OLLAMA_BASE_URL` at the top of [embedder/pipeline.py](embedder/pipeline.py).

Outputs:

| File | Contents |
|---|---|
| `output/file_labels.json` | Tags, summary, document_type, product, domain, keywords per file |
| `chroma_db/` | ChromaDB vector store (gitignored) |
| `output/pipeline_errors.json` | Extraction/tagging/embedding failures |

The pipeline is **resumable** — it skips already-processed files. Safe to interrupt and restart.

### Tiered processing strategy
- **Tier 1** (active): `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls` — LLM reads content, generates tags + keywords, full text embedded
- **Tier 2** (future): `.c`, `.h`, `.xml`, `.v`, `.vhd` — name+path indexing only, no content reading
- **Tier 3** (skip): `.sldprt`, `.sldasm`, `.cdb`, `.hdb` — binary CAD/EDA files, not embeddable

### Extraction coverage
- `.pdf` → pdfplumber (all pages)
- `.docx` → python-docx (paragraphs + tables)
- `.pptx` → python-pptx (all slide text)
- `.xlsx` → openpyxl (up to 500 rows/sheet)
- `.xls` → xlrd
- `.doc` / `.ppt` → win32com (requires Microsoft Office installed on Windows)

## Constraints

- **Read-only**: no phase should write to or move files on `R:\`.
- **All LLM calls go to the Jetson** over Tailscale — no cloud API calls.
- Phase 2 and 3 each get their own `requirements.txt` as they are built out.
