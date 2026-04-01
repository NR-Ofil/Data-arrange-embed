# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## Project Goal

Pipeline to make a large network drive (mapped as `R:\`, ~580GB, ~927K files) queryable via a local LLM:

1. **Phase 1 — Scan** (`scanner/`): Walk the drive, classify every file by type, produce JSON/CSV indexes.
2. **Phase 2+3 — Tag & Embed** (`embedder/`): Extract text from documents/presentations, tag via LLM, embed into Qdrant.
3. **Portal** (`portal/`): Local web search interface over the indexed drive.

### Drive profile (from scan, 2026-03-30)
- 52,490 target files (PDFs, Word, Excel, PowerPoint) — tagging in progress (135 done as of 2026-04-01)
- Large `other` category: `.sldprt`/`.sldasm` (SolidWorks CAD), `.cdb`/`.hdb` (EDA databases) — binary, skipped
- Top folders: `FPGA_2025`, `Electronics Design`, `DC-10`, `Mirage`, `Daycor`, `i-COR`, `CTO`, `Chemistry Lab`
- Engineering R&D company — optical/sensing instruments (UV cameras, photon counters, FPGA systems, gas detectors)
- Hebrew folder/file names present (e.g. `מפאת`, LuminarHD Hebrew docs) — pipeline handles UTF-8, Hebrew content tags correctly
- Products identified so far: DayCor, Daycor, LuminarHD, RV-ONE, ROMpact, Alpha Camera, UvolleHD, HZ-ACM-10, HZ-FA-371

## Infrastructure

- **Jetson AGX Orin** (Yahboom, 16GB) — `jetson@100.84.73.5` over Tailscale
  - Ollama: `http://100.84.73.5:11434` — exposed on `0.0.0.0` via systemd override
  - LLM model: `qwen3:8b` — best for structured JSON + multilingual
  - Embedding model: `nomic-embed-text`
  - Other models: `qwen2.5vl:3b`, `deepseek-r1:latest`, `gemma3:4b`, `llama3.2:3b`
  - Qdrant: `http://100.84.73.5:6333` — running in Docker, collection: `drive_documents`
  - Open WebUI: `http://100.84.73.5:8080`
  - SearXNG: `http://100.84.73.5:8889`
  - Free disk: ~32GB (2TB NVMe SSD pending purchase)
- **Virtual environment**: `.venv/` at project root. Use `.venv/Scripts/python.exe` or activate with `.venv\Scripts\activate`
- **Drive**: `R:\` mapped on Windows; read-only throughout all phases

## Running the Search Portal

```bash
.venv\Scripts\python.exe portal/serve.py
```

Opens `http://localhost:8765/portal/index.html` automatically. Requires `output/file_index.json` (run Phase 1 first). No third-party deps.

## Running Phase 1 (Scanner)

```bash
.venv\Scripts\python.exe scanner/scan.py            # full scan of R:\
.venv\Scripts\python.exe scanner/scan.py R:\Subdir  # test on subfolder
```

Outputs (gitignored under `output/`):

| File | Contents |
|---|---|
| `output/file_index.json` | Every file: path, ext, category, size, modified |
| `output/relevant_files.json` | Embeddable subset (docs, text, code) |
| `output/summary_by_type.csv` | Per-extension counts and sizes |
| `output/report.html` | Visual scan report — open in browser |
| `output/scan_errors.json` | Permission-denied paths |

## Running Phase 2+3 (Tag + Embed)

```bash
.venv\Scripts\python.exe embedder/pipeline.py --tag-only   # tag only, no Qdrant
.venv\Scripts\python.exe embedder/pipeline.py              # tag + embed into Qdrant
```

Pipeline is **resumable** — saves `output/file_labels.json` after every file. Safe to interrupt and restart anytime.

Live progress display: `[####----------] 12.4%  6,500/52,490  elapsed 2h 10m  ETA 15h 22m  2,980 files/h  err 3  [tagging] filename.pdf`

Outputs:

| File | Contents |
|---|---|
| `output/file_labels.json` | summary, document_type, product, domain, tags, keywords per file |
| Qdrant `drive_documents` | Chunked embeddings at `http://100.84.73.5:6333` |
| `output/pipeline_errors.json` | Extraction/tagging/embedding failures |

### Tiered processing strategy
- **Tier 1 (active)**: `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls` — LLM reads content, generates tags + keywords, full text embedded
- **Tier 2 (future)**: `.c`, `.h`, `.xml`, `.v`, `.vhd` — name+path indexing only
- **Tier 3 (skip)**: `.sldprt`, `.sldasm`, `.cdb`, `.hdb` — binary, not embeddable

### Extraction coverage
- `.pdf` → pdfplumber (all pages)
- `.docx` → python-docx (paragraphs + tables)
- `.pptx` → python-pptx (all slide text)
- `.xlsx` → openpyxl (up to 500 rows/sheet)
- `.xls` → xlrd
- `.doc` / `.ppt` → win32com (requires Microsoft Office on Windows; silently skipped if absent)

## File Classification

Defined at the top of [scanner/scan.py](scanner/scan.py):

- `EMBEDDABLE` — `document`, `text`, `code` categories passed to the pipeline
- `SKIP_EXTENSIONS` — binary/media/archive formats ignored
- `SKIP_DIRS` — folder names pruned from the walk entirely
- `EMBED_SIZE_WARN_MB = 50` — files above this flagged `large_file: true`

## Known Issues & Edge Cases

- **Windows terminal encoding**: use ASCII characters (`-`, `#`) not Unicode box-drawing chars (`─`, `█`) — Windows cp1255 terminal crashes on them. Affects all print statements in pipeline.py and status.py.
- **Corrupt timestamps**: wrap `datetime.fromtimestamp()` in `try/except (OSError, OverflowError, ValueError)` — some files on `R:\` have invalid mtimes
- **Docstrings with Windows paths**: use `R:\\` not `R:\` — bare backslash triggers `SyntaxWarning`
- **qwen3:8b think tags**: model sometimes emits `<think>...</think>` before JSON — strip with `raw[raw.rfind("</think>") + len("</think>"):].strip()`
- **Qdrant dashboard**: viewable at `http://100.84.73.5:6333/dashboard` — shows collections and vector counts
- **Embedding not yet started**: pipeline currently running `--tag-only`. Run without that flag to embed into Qdrant after tagging is complete (or at any point to embed already-tagged files)
- **status.py**: run from project root with `.venv\Scripts\python.exe status.py` to check tagging progress anytime
- **run.py**: preferred way to run the pipeline — auto-restarts on crash, prints status every hour, logs to `output/pipeline_log.txt`
  ```bash
  .venv\Scripts\python.exe run.py --tag-only   # tag only
  .venv\Scripts\python.exe run.py              # tag + embed
  ```
- **Pipeline crash pattern**: crashes on Hebrew-named files due to Windows terminal encoding. Fixed in pipeline.py with JSON extraction robustness + 3 retry attempts. run.py auto-restarts around this.

## Constraints

- **Read-only**: no phase writes to or moves files on `R:\`
- **All LLM/embedding calls go to the Jetson** — no cloud API calls
