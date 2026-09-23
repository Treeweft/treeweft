"""Backward-compatible re-export of indexer module."""
from treeweft.adapters.tree_sitter.indexer import (
    CHUNK_SIZE,
    CodeIndexer,
    LANGUAGE_MAP,
    MARKDOWN_MAP,
    SUPPORTED_EXTENSIONS,
    detect_language,
)  # noqa: F401
