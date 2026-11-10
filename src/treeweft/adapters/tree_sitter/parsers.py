"""Tree-sitter parser readiness: can every `LANGUAGE_MAP` language load?

`tree_sitter_language_pack` downloads parsers on first use. When that download
fails (offline host, GitHub release outage), `_make_chunker` silently falls
back to `TokenChunker` and a whole index is built on character windows. This
module lets the service refuse or fail index work instead, and gives the
image build a prefetch step (`python -m treeweft.adapters.tree_sitter.parsers
--prefetch`).

Deliberately imports no Milvus/Neo4j adapter (CLAUDE.md startup-path rule).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import tree_sitter_language_pack as _pack

logger = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 30.0
DEFAULT_PREFETCH_ATTEMPTS = 5


@dataclass(frozen=True)
class ParserReadiness:
    ok: bool
    failures: dict[str, str] = field(default_factory=dict)


_lock = threading.Lock()
_cached: ParserReadiness | None = None
_cached_at = 0.0
_clock: Callable[[], float] = time.monotonic


def retry_seconds() -> float:
    try:
        return float(os.environ.get("TREEWEFT_PARSER_RETRY_SECONDS", DEFAULT_RETRY_SECONDS))
    except ValueError:
        return DEFAULT_RETRY_SECONDS


def reset_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0


def _languages() -> list[str]:
    # Imported lazily: indexer.py imports this module for log_download_failure.
    from treeweft.adapters.tree_sitter.indexer import LANGUAGE_MAP

    return sorted(set(LANGUAGE_MAP.values()))


def check_parsers(force: bool = False) -> ParserReadiness:
    """Load every `LANGUAGE_MAP` language. A full success is cached for the
    process; a result with failures only for `TREEWEFT_PARSER_RETRY_SECONDS`
    so an outage doesn't start a download per request. Blocking (may download)
    — async callers use `check_parsers_async()`."""
    global _cached, _cached_at
    with _lock:
        if not force and _cached is not None:
            if _cached.ok or _clock() - _cached_at < retry_seconds():
                return _cached
        failures: dict[str, str] = {}
        for lang in _languages():
            try:
                _pack.get_language(lang)
            except Exception as exc:  # noqa: BLE001
                failures[lang] = f"{type(exc).__name__}: {exc}"
        _cached = ParserReadiness(ok=not failures, failures=failures)
        _cached_at = _clock()
        return _cached


async def check_parsers_async() -> ParserReadiness:
    return await asyncio.to_thread(check_parsers)


# ── Download-failure logging ─────────────────────────────────────────────

_logged_langs: set[str] = set()
_logged_lock = threading.Lock()


def log_download_failure(lang: str, exc: BaseException) -> None:
    """ERROR once per language per process, DEBUG afterwards."""
    with _logged_lock:
        first = lang not in _logged_langs
        _logged_langs.add(lang)
    level = logging.ERROR if first else logging.DEBUG
    logger.log(
        level,
        "tree-sitter parser download failed for %r: %s — falling back to token windows",
        lang, exc,
    )


def reset_download_log() -> None:
    with _logged_lock:
        _logged_langs.clear()


# ── CLI ──────────────────────────────────────────────────────────────────

def prefetch(
    attempts: int = DEFAULT_PREFETCH_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download every parser, retrying with exponential backoff (2s, 4s, 8s…)."""
    langs = _languages()
    for attempt in range(1, attempts + 1):
        try:
            _pack.download(langs)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"download attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
            if attempt == attempts:
                return
            sleep(2 ** attempt)


def main(argv: list[str] | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Check (and optionally prefetch) tree-sitter parsers.")
    ap.add_argument("--prefetch", action="store_true", help="download parsers first")
    ap.add_argument("--attempts", type=int, default=DEFAULT_PREFETCH_ATTEMPTS)
    args = ap.parse_args(argv)

    if args.prefetch:
        prefetch(args.attempts, sleep)
    result = check_parsers(force=True)
    print(f"cache_dir: {_pack.cache_dir()}")
    for lang in _languages():
        print(f"  {lang}: {'FAILED - ' + result.failures[lang] if lang in result.failures else 'ok'}")
    if not result.ok:
        print(f"parsers unavailable for: {', '.join(sorted(result.failures))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
