"""Jira comment and description bodies as plain text (specs/003-jira-commitments, research J1).

Jira Cloud (REST v3) returns bodies as Atlassian Document Format (ADF) JSON; self-hosted Jira
(and Cloud REST v2) returns wiki markup. Both normalizers produce the same
:class:`CommentText`, so everything above them (task comments, commitment extraction) behaves
identically for both deployments (SC-008):

- ``plain``: the author's own words. Quotes are removed; formatting markers are dropped and the
  text kept; links become their text (or the URL when there is none); list items become
  ``- item`` lines; table rows become ``a | b`` lines; code keeps its text. Whitespace is
  normalized: runs of spaces collapse to one, every line is stripped and empty lines are
  dropped, so paragraphs and line breaks are both a single ``"\\n"``.
- ``quoted``: the text of each quote (ADF ``blockquote``, wiki ``{quote}`` block or ``bq.``
  line), normalized the same way; quoted words are someone else's, never the author's promise.
- ``mentions``: the people the author mentions (``@Name`` in ``plain``), first mention first,
  one entry per id; mentions inside quotes are not the author's and are left out.

A mention's display name comes from the optional :data:`MentionResolver` (id -> name, e.g. the
issue's known participants), else from the ADF node's own text, else the raw id. Both functions
are pure and never raise on malformed input: unexpected nodes are skipped, a non-dict ADF
document or a non-string markup yields an empty :class:`CommentText`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

MentionResolver = Callable[[str], str | None]


class Mention(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str


class CommentText(BaseModel):
    model_config = ConfigDict(frozen=True)

    plain: str = ""
    quoted: tuple[str, ...] = ()
    mentions: tuple[Mention, ...] = ()


# --- shared ------------------------------------------------------------------------------------

_SPACES = re.compile(r"[ \t\f\v  -​  　]+")


def _normalize(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


@dataclass
class _Collector:
    resolve: MentionResolver | None
    quoted: list[str] = field(default_factory=list)
    mentions: dict[str, Mention] = field(default_factory=dict)

    def mention(self, mention_id: str, fallback: str | None, *, in_quote: bool) -> str:
        name: str | None = None
        if self.resolve is not None and mention_id:
            try:
                name = self.resolve(mention_id)
            except Exception:  # a resolver bug must not break body parsing
                name = None
        if not name:
            name = fallback or mention_id
        name = _normalize(name).replace("\n", " ")
        if not in_quote and mention_id and mention_id not in self.mentions:
            self.mentions[mention_id] = Mention(id=mention_id, display_name=name)
        return f"@{name}"

    def quote(self, text: str) -> None:
        normalized = _normalize(text)
        if normalized:
            self.quoted.append(normalized)

    def result(self, plain: str) -> CommentText:
        return CommentText(
            plain=_normalize(plain),
            quoted=tuple(self.quoted),
            mentions=tuple(self.mentions.values()),
        )


# --- ADF (Jira Cloud v3) -----------------------------------------------------------------------

_SKIPPED_TYPES = frozenset({"rule", "mediaSingle", "mediaGroup", "media", "mediaInline"})


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _children(node: dict[str, Any]) -> list[Any]:
    content = node.get("content")
    return content if isinstance(content, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _adf_date(timestamp: Any) -> str:
    try:
        millis = int(timestamp)
        return datetime.fromtimestamp(millis / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class _AdfRenderer:
    def __init__(self, collector: _Collector) -> None:
        self._c = collector

    def inner(self, node: dict[str, Any], *, in_quote: bool) -> str:
        return "".join(self.render(child, in_quote=in_quote) for child in _children(node))

    def render(self, node: Any, *, in_quote: bool) -> str:
        if not isinstance(node, dict):
            return ""
        kind = node.get("type")
        attrs = _attrs(node)
        match kind:
            case "text":
                return _str(node.get("text"))
            case "hardBreak":
                return "\n"
            case "mention":
                text = _str(attrs.get("text")).removeprefix("@")
                return self._c.mention(_str(attrs.get("id")), text or None, in_quote=in_quote)
            case "emoji":
                return _str(attrs.get("text")) or _str(attrs.get("shortName"))
            case "inlineCard":
                return _str(attrs.get("url"))
            case "blockCard" | "embedCard":
                return f"\n{_str(attrs.get('url'))}\n"
            case "date":
                return _adf_date(attrs.get("timestamp"))
            case "status":
                return _str(attrs.get("text"))
            case "bulletList" | "orderedList":
                return "".join(self._list_item(item, in_quote=in_quote) for item in _children(node))
            case "listItem":
                return self._list_item(node, in_quote=in_quote)
            case "blockquote":
                if in_quote:
                    return f"\n{self.inner(node, in_quote=True)}\n"
                self._c.quote(self.inner(node, in_quote=True))
                return "\n"
            case "table":
                rows = (self.render(row, in_quote=in_quote) for row in _children(node))
                return "\n" + "\n".join(row for row in rows if row) + "\n"
            case "tableRow":
                cells = [
                    _normalize(self.inner(cell, in_quote=in_quote)).replace("\n", " ")
                    for cell in _children(node)
                    if isinstance(cell, dict)
                ]
                return " | ".join(cells)
            case _ if kind in _SKIPPED_TYPES:
                return ""
            case _:
                # doc, paragraph, heading, codeBlock, panel, and unknown (or newer) node
                # types: a block holding whatever text its content has.
                return f"\n{self.inner(node, in_quote=in_quote)}\n"

    def _list_item(self, node: Any, *, in_quote: bool) -> str:
        if not isinstance(node, dict):
            return ""
        text = _normalize(self.inner(node, in_quote=in_quote))
        if not text:
            return ""
        first, _, rest = text.partition("\n")
        if not first.startswith("- "):
            first = f"- {first}"
        return f"\n{first}\n{rest}\n"


def adf_to_text(doc: Any, resolve: MentionResolver | None = None) -> CommentText:
    """An ADF document (Jira Cloud v3 comment or description body) as :class:`CommentText`."""
    collector = _Collector(resolve)
    if not isinstance(doc, dict):
        return CommentText()
    plain = _AdfRenderer(collector).render(doc, in_quote=False)
    return collector.result(plain)


# --- wiki markup (self-hosted Jira, Cloud v2) --------------------------------------------------

_WIKI_BLOCK = re.compile(
    r"\{(?P<kind>code|noformat|quote)(?::[^}]*)?\}(?P<body>.*?)\{(?P=kind)\}",
    re.DOTALL,
)
_LEFTOVER_MACRO = re.compile(
    r"\{(?:code|noformat|quote|panel|color|section|column|anchor)(?::[^}]*)?\}"
)
_LINE_BREAK = re.compile(r"\\\\")
_MENTION = re.compile(r"\[~(?:accountid:)?([^\]\s]+)\]")
_LINK_WITH_TEXT = re.compile(r"\[([^\[\]|]*)\|([^\[\]]*)\]")
_BARE_LINK = re.compile(r"\[((?:[a-zA-Z][a-zA-Z0-9+.-]*://|mailto:|file:|#|\^)[^\[\]|]*)\]")
_MONO = re.compile(r"\{\{(.*?)\}\}")
_HEADING = re.compile(r"^h[1-6]\.\s+")
_BQ = re.compile(r"^bq\.\s+")
_LIST = re.compile(r"^[*#-]+\s+")
_RULE = re.compile(r"^-{4,}$")
_TOKEN_OPEN, _TOKEN_CLOSE = "\ue000", "\ue001"  # private-use characters
_TOKEN = re.compile(f"{_TOKEN_OPEN}(\\d+){_TOKEN_CLOSE}")
# Emphasis markers: an opening marker at a word boundary followed by a non-space, a closing one
# after a non-space and before a non-word character. ``well-known``, ``2026-11-05``, ``C++``,
# ``snake_case`` and ``a - b`` are left alone.
_EMPHASIS = tuple(
    re.compile(rf"(?<![\w{m}])\{m}(?=[^\s{m}])(.+?)(?<=[^\s{m}])\{m}(?![\w{m}])")
    for m in ("*", "_", "-", "+", "^", "~")
) + (re.compile(r"(?<!\w)\?\?(?=\S)(.+?)(?<=\S)\?\?(?!\w)"),)


class _WikiRenderer:
    def __init__(self, collector: _Collector) -> None:
        self._c = collector

    def render(self, markup: str, *, in_quote: bool) -> str:
        out: list[str] = []
        pos = 0
        for match in _WIKI_BLOCK.finditer(markup):
            out.append(self._lines(markup[pos : match.start()], in_quote=in_quote))
            body = match.group("body")
            if match.group("kind") == "quote":
                text = self.render(body, in_quote=True)
                if in_quote:
                    out.append(f"\n{text}\n")
                else:
                    self._c.quote(text)
                    out.append("\n")
            else:
                out.append(f"\n{body}\n")
            pos = match.end()
        out.append(self._lines(markup[pos:], in_quote=in_quote))
        return "".join(out)

    def _lines(self, segment: str, *, in_quote: bool) -> str:
        segment = _LINE_BREAK.sub("\n", segment)
        lines: list[str] = []
        for raw in segment.replace("\r", "\n").split("\n"):
            line = raw.strip()
            if not line or _RULE.fullmatch(line):
                continue
            if _BQ.match(line):
                text = self._inline(_BQ.sub("", line), in_quote=True)
                if in_quote:
                    lines.append(text)
                else:
                    self._c.quote(text)
                continue
            line = _HEADING.sub("", line)
            if line.startswith("|"):
                lines.append(self._table_row(line, in_quote=in_quote))
                continue
            if _LIST.match(line):
                lines.append(f"- {self._inline(_LIST.sub('', line), in_quote=in_quote)}")
                continue
            lines.append(self._inline(line, in_quote=in_quote))
        return "\n" + "\n".join(lines) + "\n"

    def _table_row(self, line: str, *, in_quote: bool) -> str:
        text = self._inline(line, in_quote=in_quote)
        cells = text.replace("||", "|").strip().strip("|").split("|")
        return " | ".join(_normalize(cell).replace("\n", " ") for cell in cells)

    def _inline(self, text: str, *, in_quote: bool) -> str:
        # Mentions, URLs and monospace text are set aside as tokens so the emphasis pass
        # cannot eat underscores or hyphens inside them.
        protected: list[str] = []

        def protect(value: str) -> str:
            protected.append(value)
            return f"{_TOKEN_OPEN}{len(protected) - 1}{_TOKEN_CLOSE}"

        def mention(match: re.Match[str]) -> str:
            return protect(self._c.mention(match.group(1), None, in_quote=in_quote))

        def link(match: re.Match[str]) -> str:
            label, url = match.group(1).strip(), match.group(2).strip()
            return label if label else protect(url)

        text = _MENTION.sub(mention, text)
        text = _LINK_WITH_TEXT.sub(link, text)
        text = _BARE_LINK.sub(lambda m: protect(m.group(1).strip()), text)
        text = _MONO.sub(lambda m: protect(m.group(1)), text)
        text = _LEFTOVER_MACRO.sub("", text)
        for pattern in _EMPHASIS:
            previous = None
            while previous != text:  # nested markers: *_both_*
                previous = text
                text = pattern.sub(r"\1", text)
        return _TOKEN.sub(lambda m: protected[int(m.group(1))], text)


def wiki_to_text(markup: str, resolve: MentionResolver | None = None) -> CommentText:
    """A Jira wiki markup body (self-hosted Jira, Cloud v2) as :class:`CommentText`."""
    collector = _Collector(resolve)
    if not isinstance(markup, str):
        return CommentText()
    plain = _WikiRenderer(collector).render(markup, in_quote=False)
    return collector.result(plain)


__all__ = ["CommentText", "Mention", "MentionResolver", "adf_to_text", "wiki_to_text"]
