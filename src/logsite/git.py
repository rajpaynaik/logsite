"""Git correlation: which commits touched the code around a log line (``git log -L``).

When a line spikes in production, the commits that recently changed the function it lives in
are the first suspects. Everything here shells out to ``git``; no extra dependencies.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import parse_iso

_SEP = "\x1f"
_FORMAT = _SEP.join(["%H", "%an", "%aI", "%s"])


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    date: datetime
    summary: str

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    def format(self) -> str:
        return f"{self.short_sha}  {self.date:%Y-%m-%d}  {self.author}: {self.summary}"


def enclosing_block(source: str, line: int) -> tuple[int, int]:
    """Return the line range of the innermost function containing ``line``.

    A log line usually spikes because the code that calls it changed, not the log call
    itself, so the whole function is the useful unit. Falls back to the line alone.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return line, line
    best: tuple[int, int] = (line, line)
    best_size: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            if start <= line <= end and (best_size is None or end - start < best_size):
                best, best_size = (start, end), end - start
    return best


def parse_log(output: str) -> list[Commit]:
    """Parse ``git log --format=_FORMAT`` output (one commit per line)."""
    commits: list[Commit] = []
    for raw in output.splitlines():
        parts = raw.split(_SEP)
        if len(parts) != 4:
            continue  # blank separators or diff output
        sha, author, date, summary = parts
        try:
            when = parse_iso(date)
        except ValueError:
            continue
        commits.append(Commit(sha=sha, author=author, date=when, summary=summary))
    return commits


def _git(root: str | Path, *args: str) -> str | None:
    """stdout of ``git -C root <args>``, or None if git is missing or the command fails."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def line_history(
    root: str | Path,
    path: str,
    start: int,
    end: int | None = None,
    since: datetime | None = None,
    max_count: int = 5,
    revs: str | None = None,
) -> list[Commit]:
    """Commits (newest first) that touched lines ``start..end`` of ``path`` at ``HEAD``.

    ``revs`` limits the walk, e.g. ``"v1.3..HEAD"`` (it must end at ``HEAD`` for the line
    numbers to apply). Line numbers refer to ``HEAD``; uncommitted edits above the range
    shift it. Returns an empty list if ``root`` isn't a git repo, the file isn't committed,
    or git is missing.
    """
    args = [
        "log",
        f"-L{start},{end or start}:{path}",
        "--no-patch",
        f"--format={_FORMAT}",
        f"--max-count={max_count}",
    ]
    if since is not None:
        args.append(f"--since={since.isoformat()}")
    if revs:
        args.append(revs)
    out = _git(root, *args)
    return parse_log(out) if out is not None else []


def source_url(root: str | Path, remote: str = "origin") -> str | None:
    """A link template for a source line on the code host, with ``{ref}``, ``{path}`` and
    ``{line}`` placeholders, from the remote's URL (GitHub, GitLab, Bitbucket). None if
    there's no such remote or the host is unknown."""
    out = _git(root, "remote", "get-url", remote)
    if not out:
        return None
    url = out.strip().removesuffix(".git")
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    url = re.sub(r"^https?://[^@/]+@", "https://", url)  # drop credentials
    if not url.startswith(("https://", "http://")):
        return None
    if "gitlab" in url:
        return url + "/-/blob/{ref}/{path}#L{line}"
    if "bitbucket" in url:
        return url + "/src/{ref}/{path}#lines-{line}"
    return url + "/blob/{ref}/{path}#L{line}"  # GitHub, Gitea, and most others


def resolve_ref(root: str | Path, ref: str) -> str | None:
    """Full commit sha for ``ref`` (branch, tag, sha, ``HEAD~2`` ...), or None."""
    out = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if out else None


def ref_time(root: str | Path, ref: str) -> datetime | None:
    """When ``ref`` was released: the tag date for an annotated tag, else the commit date."""
    out = _git(root, "for-each-ref", "--format=%(taggerdate:iso-strict)", f"refs/tags/{ref}")
    if not (out and out.strip()):
        out = _git(root, "log", "-1", "--format=%cI", ref)
    try:
        return parse_iso(out) if out and out.strip() else None
    except ValueError:
        return None


def previous_tag(root: str | Path, ref: str) -> str | None:
    """The nearest tag before ``ref`` (not ``ref`` itself), e.g. the previous release."""
    out = _git(root, "describe", "--tags", "--abbrev=0", f"{ref}^")
    return out.strip() if out else None


def tags_by_date(root: str | Path) -> list[tuple[str, datetime]]:
    """Every tag with its release time (tagger date, or the commit date), oldest first."""
    out = _git(
        root,
        "for-each-ref",
        "--sort=creatordate",
        "--format=%(refname:short)%09%(creatordate:iso-strict)",
        "refs/tags",
    )
    tags = []
    for line in (out or "").splitlines():
        name, _, when = line.partition("\t")
        try:
            tags.append((name, parse_iso(when)))
        except ValueError:
            continue
    return tags


def rev_list(root: str | Path, *revs: str) -> list[str]:
    """Commit shas in ``revs`` (e.g. ``"v1.3..v1.4"``, or ``"--since=...", "v1.4"``)."""
    out = _git(root, "rev-list", *revs)
    return out.split() if out else []


def parse_target(target: str, root: str | Path, base: str | Path | None = None) -> tuple[str, int]:
    """Turn ``PATH:LINE`` into a root-relative ``(path, line)``.

    A relative PATH is resolved against ``base`` (default: the current directory).

    Raises ValueError with a user-facing message.
    """
    path, _, line = target.rpartition(":")
    if not path or not line.isdigit():
        raise ValueError(f"expected PATH:LINE, e.g. app/checkout.py:42, got {target!r}")
    try:
        rel = (Path(base or Path.cwd()) / path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise ValueError(f"{path} is outside the project root {root}") from None
    return rel.as_posix(), int(line)


def site_history(
    root: str | Path,
    path: str,
    line: int,
    since: datetime | None = None,
    max_count: int = 5,
    revs: str | None = None,
) -> list[Commit]:
    """Commits that touched the function around the log call at ``path:line``."""
    try:
        source = (Path(root) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    start, end = enclosing_block(source, line)
    return line_history(root, path, start, end, since=since, max_count=max_count, revs=revs)
