"""Find log calls in TypeScript and JavaScript: ``console``, pino and bunyan
(``logger.info({ orderId }, "order %s failed", id)``), winston (``logger.info("msg", meta)``,
``logger.log("warn", "msg")``), NestJS's ``Logger`` and log4js, including child loggers
(``logger.child({ requestId })``) and loggers on objects (``this.logger``, ``req.log``)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..models import LogSite
from .base import CallFacts, parse, text

LIBRARIES = {
    "pino": "pino", "pino-http": "pino", "nestjs-pino": "pino", "winston": "winston",
    "bunyan": "bunyan", "@nestjs/common": "nestjs", "log4js": "log4js", "loglevel": "loglevel",
    "consola": "consola", "tslog": "tslog", "roarr": "roarr",
}  # fmt: skip
LEVELS = {
    "trace": "debug", "debug": "debug", "verbose": "debug", "silly": "debug", "info": "info",
    "http": "info", "log": "info", "warn": "warning", "warning": "warning", "error": "error",
    "fatal": "critical", "crit": "critical", "critical": "critical",
}  # fmt: skip
CONSOLE = {"log", "info", "debug", "trace", "warn", "error"}
LOGGER_NAME = re.compile(r"(?i)^(log|logger|\w*logger|_log|_logger|\$log)$")
ERR_NAME = re.compile(r"^(e|err|error|ex|exc|exception|\w*Err|\w*Error|\w*Exception)$")
FACTORIES = {"pino", "createLogger", "getLogger", "child", "createConsola", "bunyan"}
LOOPS = {"for_statement", "for_in_statement", "while_statement", "do_statement"}
SCOPES = {
    "function_declaration", "function_expression", "arrow_function", "method_definition",
    "generator_function_declaration", "class_body",
}  # fmt: skip
# util.format's placeholders that the matcher doesn't know: "%j" (JSON), "%O" (object)
_FORMAT_ONLY = re.compile(r"%[jO]")


@dataclass(frozen=True)
class _Ctx:
    in_loop: bool = False
    catch_param: str | None = None
    swallowing: bool = False  # a catch that never rethrows


def _args(call: Any) -> list[Any]:
    node = call.child_by_field_name("arguments")
    return [c for c in node.named_children if c.type != "comment"] if node else []


def string_value(node: Any) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        out = []
        for c in node.named_children:
            if c.type == "string_fragment":
                out.append(text(c))
            elif c.type == "escape_sequence":
                out.append(bytes(text(c), "utf-8").decode("unicode_escape", errors="replace"))
        return "".join(out)
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.named_children
    ):
        return text(node)[1:-1]
    return None


def _template_literal(node: Any) -> str:
    """```order ${id} failed``` -> ``"order {} failed"``."""
    raw = node.text
    out, pos = [], node.start_byte + 1
    for c in node.named_children:
        if c.type == "template_substitution":
            out.append(raw[pos - node.start_byte : c.start_byte - node.start_byte].decode())
            out.append("{}")
            pos = c.end_byte
    out.append(raw[pos - node.start_byte : len(raw) - 1].decode())
    return "".join(out)


def _concat(node: Any) -> list[Any]:
    if node.type == "binary_expression" and text(node.child_by_field_name("operator")) == "+":
        return _concat(node.child_by_field_name("left")) + _concat(
            node.child_by_field_name("right")
        )
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return _concat(node.named_children[0])
    return [node]


def message_template(node: Any) -> tuple[str | None, str | None]:
    """``(template, eager kind)`` for a message argument."""
    value = string_value(node)
    if value is not None:
        return _FORMAT_ONLY.sub("%s", value), None
    if node.type == "template_string":
        return _template_literal(node), "a template literal"
    if node.type == "binary_expression":
        parts = _concat(node)
        values = [string_value(p) if p.type != "template_string" else _template_literal(p)
                  for p in parts]  # fmt: skip
        if any(v is not None for v in values):
            if all(v is not None for v in values) and not any(
                p.type == "template_string" for p in parts
            ):
                return "".join(values), None
            return "".join("{}" if v is None else v for v in values), "string concatenation"
    return None, None


def _identifiers(node: Any) -> set[str]:
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("identifier", "property_identifier", "shorthand_property_identifier"):
            out.add(text(n))
        if n.type not in ("string", "template_string"):
            stack.extend(n.named_children)
    return out


def _object_keys(node: Any) -> set[str]:
    """The keys of an object literal: ``{ orderId, user: u }`` -> {"orderId", "user"}."""
    keys = set()
    for c in node.named_children:
        if c.type == "shorthand_property_identifier":
            keys.add(text(c))
        elif c.type == "pair":
            key = c.child_by_field_name("key")
            keys.add(string_value(key) or text(key))
    return keys


def _contains(node: Any, kind: str) -> bool:
    stack = list(node.named_children)
    while stack:
        n = stack.pop()
        if n.type == kind:
            return True
        if n.type not in SCOPES:
            stack.extend(n.named_children)
    return False


class _Indexer:
    def __init__(self, path: str, root: Any) -> None:
        self.path = path
        self.out: list[tuple[LogSite, CallFacts]] = []
        self.loggers: set[str] = set()
        self.libraries: set[str] = set()
        self._scan(root)
        self.library = next(iter(sorted(self.libraries)), "console")

    def _scan(self, root: Any) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type == "import_statement":
                source = string_value(node.child_by_field_name("source"))
                if source in LIBRARIES:
                    self.libraries.add(LIBRARIES[source])
            elif node.type == "call_expression" and text(node.child_by_field_name("function")) in (
                "require", "import"):  # fmt: skip
                args = _args(node)
                if args and string_value(args[0]) in LIBRARIES:
                    self.libraries.add(LIBRARIES[string_value(args[0])])
            elif node.type in ("variable_declarator", "public_field_definition",
                               "field_definition"):  # fmt: skip
                name = node.child_by_field_name("name") or node.child_by_field_name("property")
                value = node.child_by_field_name("value")
                if name is not None and value is not None and self._makes_logger(value):
                    self.loggers.add(text(name))

    def _makes_logger(self, value: Any) -> bool:
        if value.type == "new_expression":  # new Logger(OrderService.name) (NestJS)
            return text(value.child_by_field_name("constructor")).endswith("Logger")
        if value.type == "call_expression":
            fn = value.child_by_field_name("function")
            name = text(fn.child_by_field_name("property")) if fn.type == "member_expression" \
                else text(fn)  # fmt: skip
            return name in FACTORIES
        return False

    def is_logger(self, node: Any | None) -> bool:
        if node is None:
            return False
        if node.type == "identifier":
            name = text(node)
            return name in self.loggers or bool(LOGGER_NAME.match(name))
        if node.type == "member_expression":  # this.logger, req.log, app.log
            prop = text(node.child_by_field_name("property"))
            return prop in self.loggers or bool(LOGGER_NAME.match(prop))
        if node.type == "call_expression":  # logger.child({ id }).info(...)
            return self._makes_logger(node)
        return False

    def visit(self, node: Any, ctx: _Ctx) -> None:
        kind = node.type
        if kind in SCOPES:
            ctx = _Ctx()
        elif kind in LOOPS:
            ctx = _Ctx(True, ctx.catch_param, ctx.swallowing)
        elif kind == "catch_clause":
            param = node.child_by_field_name("parameter")
            body = node.child_by_field_name("body")
            rethrows = body is not None and _contains(body, "throw_statement")
            ctx = _Ctx(ctx.in_loop, text(param) if param is not None else None, not rethrows)
        if kind == "call_expression":
            self.call(node, ctx)
        for child in node.named_children:
            self.visit(child, ctx)

    def call(self, node: Any, ctx: _Ctx) -> None:
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            return
        obj = fn.child_by_field_name("object")
        method = text(fn.child_by_field_name("property"))
        args = _args(node)
        if obj is not None and obj.type == "identifier" and text(obj) == "console":
            if method not in CONSOLE:
                return
            library = "console"
        elif method in LEVELS and self.is_logger(obj):
            library = self.library
        else:
            return
        level = LEVELS[method]
        message = args[0] if args else None
        keys: set[str] = set()
        rest = args[1:]
        if method == "log" and library == "winston" and len(args) >= 2:
            lvl = string_value(args[0])  # logger.log("warn", "msg", meta)
            if lvl in LEVELS:
                level, message, rest = LEVELS[lvl], args[1], args[2:]
        if message is not None and message.type in ("object", "identifier", "member_expression") \
                and library in ("pino", "bunyan", "console") and len(args) >= 2:  # fmt: skip
            # pino / bunyan: logger.info({ orderId }, "msg") or logger.error(err, "msg")
            if message.type == "object":
                keys |= _object_keys(message)
            rest, message = [message, *args[2:]], args[1]
        elif message is not None and message.type == "object" and len(args) == 1:
            keys |= _object_keys(message)  # logger.info({ msg: "...", orderId })
            msg = next((c.child_by_field_name("value") for c in message.named_children
                        if c.type == "pair" and text(c.child_by_field_name("key")) in (
                            "msg", "message")), None)  # fmt: skip
            rest, message = [message], msg
        self.add(node, level, message, rest, keys, ctx, library)

    def add(self, node: Any, level: str, message: Any, rest: list[Any], keys: set[str],
            ctx: _Ctx, library: str) -> None:  # fmt: skip
        facts = CallFacts(language="typescript", names=set(keys))
        template = None
        if message is not None:
            template, facts.eager = message_template(message)
            facts.dynamic = template is None
            if library == "console" and facts.eager == "a template literal":
                facts.eager = None  # idiomatic with console, and still matched by template
            if facts.eager:
                facts.names |= _identifiers(message)
            if template is None and self._is_exception(message, ctx):
                facts.has_exception, facts.dynamic = True, False  # logger.error(err)
        for arg in rest:
            facts.names |= _identifiers(arg)
            if self._is_exception(arg, ctx):
                facts.has_exception = True
            elif arg.type == "member_expression" and self._is_exception(
                arg.child_by_field_name("object"), ctx
            ):
                if text(arg.child_by_field_name("property")) == "stack":
                    facts.has_exception = True  # err.stack: the stack trace is logged
                else:
                    facts.exception_message_only = True  # err.message
            elif arg.type == "object":
                for c in arg.named_children:
                    value = c.child_by_field_name("value") if c.type == "pair" else c
                    if value is not None and self._is_exception(value, ctx):
                        facts.has_exception = True  # { err }, { error: e }
        logger = node.child_by_field_name("function").child_by_field_name("object")
        site = LogSite(
            path=self.path,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            end_col=node.end_point[1],
            level=level,
            template=template,
            logger=text(logger).split("(")[0][:80],
            in_except=ctx.catch_param is not None,
            in_loop=ctx.in_loop,
            in_swallowing_except=ctx.swallowing,
            library=library,
            bound_fields=tuple(sorted(keys)),
        )
        self.out.append((site, facts))

    @staticmethod
    def _is_exception(node: Any | None, ctx: _Ctx) -> bool:
        if node is None:
            return False
        if node.type in ("identifier", "shorthand_property_identifier"):
            name = text(node)
            return name == ctx.catch_param or bool(ERR_NAME.match(name))
        if node.type == "new_expression":
            return text(node.child_by_field_name("constructor")).endswith(("Error", "Exception"))
        return False


def index(source: str, path: str) -> list[tuple[LogSite, CallFacts]]:
    if path.endswith((".min.js", ".bundle.js")):
        return []  # built output, not the project's code
    grammar = "tsx" if path.endswith((".tsx", ".jsx")) else "typescript"
    root = parse(grammar, source)
    indexer = _Indexer(path, root)
    indexer.visit(root, _Ctx())
    return indexer.out
