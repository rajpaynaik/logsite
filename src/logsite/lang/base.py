"""What the tree-sitter based indexers (Java, Go, TypeScript) share: parsing, and the facts
about each log call that the language-neutral lint rules check."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

# Placeholders that mean the same as SLF4J's "{}" (java.util.logging, MessageFormat).
_NUMBERED = re.compile(r"\{\d+\}")


class MissingParser(RuntimeError):
    """The tree-sitter grammar for a language isn't installed."""


@cache
def parser_for(language: str) -> Any:
    try:
        from tree_sitter import Language, Parser

        if language == "java":
            import tree_sitter_java as grammar
        elif language == "go":
            import tree_sitter_go as grammar
        elif language in ("typescript", "tsx"):
            import tree_sitter_typescript as ts

            build = ts.language_tsx if language == "tsx" else ts.language_typescript
            return Parser(Language(build()))
        else:
            raise MissingParser(f"no parser for {language}")
    except ImportError as exc:
        package = "typescript" if language == "tsx" else language
        raise MissingParser(
            f"logsite needs tree-sitter to read {language} code: "
            f"pip install tree-sitter tree-sitter-{package}"
        ) from exc
    return Parser(Language(grammar.language()))


def parse(language: str, source: str) -> Any:
    return parser_for(language).parse(source.encode("utf-8")).root_node


def text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def numbered_to_braces(template: str) -> str:
    """``"order {0} failed"`` -> ``"order {} failed"``."""
    return _NUMBERED.sub("{}", template)


@dataclass
class CallFacts:
    """What the language-neutral rules need to know about one log call."""

    language: str
    eager: str | None = None  # how the message is built eagerly ("string concatenation"...)
    names: set[str] = field(default_factory=set)  # identifiers and field keys that are logged
    has_exception: bool = False  # the exception / error itself is attached
    exception_message_only: bool = False  # e.getMessage(): the text without the stack trace
    dynamic: bool = False  # the message isn't a literal, so it can't be matched
    printf: bool = False  # Infof-style: values go in as format arguments
