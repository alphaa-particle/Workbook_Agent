# License Compliance Notes

This project includes a license audit utility:

- Run: `python scripts/audit_licenses.py`
- Output: `data/license_audit.json`

## Current policy mode

This repository runs in **commercial-permissive mode** (Apache-2.0/MIT/BSD only).

All dependencies use commercially-friendly licenses:
- **pypdfium2**: BSD-3/Apache-2.0 (PDF text extraction)
- **RapidOCR**: Apache-2.0 (ONNX-based OCR)
- **fastembed**: Apache-2.0 (text embeddings)
- **LanceDB**: Apache-2.0 (vector store)
- **python-docx**: MIT (DOCX parsing)
- **python-calamine**: MIT (Excel/ODS parsing)
- **FastAPI/Uvicorn**: MIT (web framework)
- **aiosqlite**: MIT (async SQLite)

## Known exceptions

None — all previous non-permissive dependencies (docling, pymupdf4llm, transformers)
have been removed.

## Release gate recommendation

Before release, require:

1. `scripts/audit_licenses.py` exit code is `0` (no denied licenses), and
2. all `review`/`unknown` entries in `data/license_audit.json` have legal sign-off.
