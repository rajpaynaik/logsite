"""Languages logsite reads. Python uses the standard library's ``ast`` (``index.py``,
``lint.py``); Java, Go and TypeScript / JavaScript use tree-sitter grammars and share the
language-neutral lint rules (``rules.py``). Everything downstream (matching, counts, cost,
errors) works on ``file:line`` and doesn't care which language a log call is in."""

from __future__ import annotations

from pathlib import PurePath

from ..models import LogSite
from .base import CallFacts, MissingParser

EXTENSIONS = {
    ".py": "python", ".java": "java", ".go": "go",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "typescript", ".jsx": "typescript", ".mjs": "typescript", ".cjs": "typescript",
}  # fmt: skip


def language_of(path: str) -> str:
    """The language of a file by its extension (Python for anything unknown)."""
    return EXTENSIONS.get(PurePath(path).suffix, "python")


def index_calls(language: str, source: str, path: str) -> list[tuple[LogSite, CallFacts]]:
    """Log calls in a Java, Go or TypeScript / JavaScript source, with the facts the lint
    rules check. A file that doesn't parse yields what tree-sitter could recover, as an
    editor does."""
    if language == "java":
        from . import java

        return java.index(source, path)
    if language == "go":
        from . import go

        return go.index(source, path)
    if language == "typescript":  # and JavaScript: the TypeScript grammar reads both
        from . import typescript

        return typescript.index(source, path)
    raise ValueError(f"not a tree-sitter language: {language}")


__all__ = ["EXTENSIONS", "CallFacts", "MissingParser", "index_calls", "language_of"]
