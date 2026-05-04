"""CLI entry point for Calux Book.

Usage:
    calux-book --server           Start the API server
    calux-book --ingest           Ingest data directory sources
    calux-book --version          Print version
"""

from __future__ import annotations

# ── Hugging-Face hub safety ──────────────────────────────────────────────
# Prevent [WinError 1314] symlink crashes on Windows without Developer Mode
# and suppress the noisy warning.  Must be set before importing any HF code.
import os as _os
import sys as _sys
from pathlib import Path as _Path

_os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
# Force standard file copy instead of symlinks on Windows
if _os.name == "nt":
    _os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    _os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

# Allow direct script execution: `python calux_book/main.py`
# by ensuring the project root is on sys.path for absolute imports.
if __package__ in {None, ""}:
    _project_root = _Path(__file__).resolve().parent.parent
    _project_root_str = str(_project_root)
    if _project_root_str not in _sys.path:
        _sys.path.insert(0, _project_root_str)
# ─────────────────────────────────────────────────────────────────────────

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _setup_logging(level: str = "INFO") -> None:
    """Configure root logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _run_ingest(data_dir: str) -> None:
    """Ingest files from a directory into the vector store."""
    try:
        from .config import get_settings
        from .store import Store
        from .vector_store import VectorStore
    except ImportError:
        from calux_book.config import get_settings
        from calux_book.store import Store
        from calux_book.vector_store import VectorStore

    cfg = get_settings()
    store = Store(cfg.store_path)
    await store.initialize()
    vs = VectorStore(cfg)

    path = Path(data_dir)
    if not path.exists():
        print(f"Error: directory {data_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting files from {path} ...")
    count = 0
    for fp in path.rglob("*"):
        if fp.is_file():
            try:
                content = vs.extract_document(str(fp))
                if content:
                    vs.ingest_source("ingest", fp.name, fp.name, content)
                    count += 1
                    print(f"  Ingested: {fp.name}")
            except Exception as e:
                print(f"  Skipped {fp.name}: {e}", file=sys.stderr)

    stats = vs.get_stats()
    print(f"\nDone. Ingested {count} files, {stats.total_documents} total chunks.")
    await store.close()


def _run_server(host: str, port: int) -> None:
    """Start the FastAPI server with uvicorn."""
    import uvicorn

    try:
        from .config import get_settings, validate_settings
    except ImportError:
        from calux_book.config import get_settings, validate_settings

    cfg = get_settings()
    validate_settings(cfg)

    _setup_logging(cfg.log_level)
    logger = logging.getLogger("calux_book")
    logger.info("Starting Calux Book server on %s:%d", host, port)

    uvicorn.run(
        "calux_book.server:create_app",
        host=host,
        port=port,
        factory=True,
        log_level=cfg.log_level.lower(),
    )


def main() -> None:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(
        prog="calux-book",
        description="Calux Book — Privacy-first AI Notebook",
    )
    parser.add_argument("--server", action="store_true", help="Start the API server")
    parser.add_argument("--ingest", type=str, metavar="DIR", help="Ingest a directory of files")
    parser.add_argument("--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Server port (default: 8080)")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args()

    if args.version:
        try:
            from . import __version__
        except ImportError:
            from calux_book import __version__
        print(f"Calux Book v{__version__}")
        return

    if args.ingest:
        _setup_logging()
        asyncio.run(_run_ingest(args.ingest))
        return

    if args.server:
        _run_server(args.host, args.port)
        return

    # Default: start server
    _run_server(args.host, args.port)


if __name__ == "__main__":
    main()