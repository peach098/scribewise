"""A tiny neutral comment model rendered to both Jira body formats (SC-008 test fixtures).

Describe a comment once, then build the Cloud v3 body (ADF JSON, :func:`to_adf`) and the
wiki markup body (self-hosted Jira and Cloud v2, :func:`to_wiki`) from it, so a test can run the
same scenario against both flavors::

    body = [Para(Text("Sure, "), MentionPart("u1", "Anna Testova"), Text(" by Friday"))]
    to_adf(body)               # {"type": "doc", "version": 1, "content": [...]}
    to_wiki(body, cloud=True)  # "Sure, [~accountid:u1] by Friday"
    to_wiki(body, cloud=False) # "Sure, [~u1] by Friday"

Inline parts: :class:`Text`, :class:`Bold`, :class:`MentionPart`, :class:`Link`. Blocks:
:class:`Para`, :class:`Quote`, :class:`Bullets`, :class:`Code`, :class:`Panel`, :class:`Table`.
A bare ``str`` is accepted wherever a part or a block is expected (plain text / a paragraph).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Bold:
    text: str


@dataclass(frozen=True)
class MentionPart:
    id: str  # Cloud accountId, or the self-hosted username / user key
    name: str


@dataclass(frozen=True)
class Link:
    text: str
    url: str


Part = Text | Bold | MentionPart | Link | str


class Para:
    def __init__(self, *parts: Part) -> None:
        self.parts = parts


class Quote:
    def __init__(self, *paras: Para | str) -> None:
        self.paras = paras


class Bullets:
    def __init__(self, *items: Para | str) -> None:
        self.items = items


@dataclass(frozen=True)
class Code:
    text: str


class Panel:
    def __init__(self, *paras: Para | str) -> None:
        self.paras = paras


class Table:
    """The first row is the header row."""

    def __init__(self, *rows: tuple[str, ...]) -> None:
        self.rows = rows


Block = Para | Quote | Bullets | Code | Panel | Table | str


def _para(value: Para | str) -> Para:
    return value if isinstance(value, Para) else Para(value)


# --- ADF ---------------------------------------------------------------------------------------


def _adf_part(part: Part) -> dict[str, Any]:
    match part:
        case str():
            return {"type": "text", "text": part}
        case Text(text):
            return {"type": "text", "text": text}
        case Bold(text):
            return {"type": "text", "text": text, "marks": [{"type": "strong"}]}
        case MentionPart(id_, name):
            return {"type": "mention", "attrs": {"id": id_, "text": f"@{name}"}}
        case Link(text, url):
            return {
                "type": "text",
                "text": text,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            }
    raise TypeError(part)


def _adf_para(value: Para | str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [_adf_part(p) for p in _para(value).parts]}


def _adf_block(block: Block) -> dict[str, Any]:
    match block:
        case str() | Para():
            return _adf_para(block)
        case Quote():
            return {"type": "blockquote", "content": [_adf_para(p) for p in block.paras]}
        case Bullets():
            items = [{"type": "listItem", "content": [_adf_para(i)]} for i in block.items]
            return {"type": "bulletList", "content": items}
        case Code(text):
            return {"type": "codeBlock", "content": [{"type": "text", "text": text}]}
        case Panel():
            content = [_adf_para(p) for p in block.paras]
            return {"type": "panel", "attrs": {"panelType": "info"}, "content": content}
        case Table():
            rows = []
            for index, row in enumerate(block.rows):
                cell_type = "tableHeader" if index == 0 else "tableCell"
                cells = [{"type": cell_type, "content": [_adf_para(c)]} for c in row]
                rows.append({"type": "tableRow", "content": cells})
            return {"type": "table", "content": rows}
    raise TypeError(block)


def to_adf(blocks: Sequence[Block]) -> dict[str, Any]:
    """The comment as an ADF document (Jira Cloud REST v3 ``body``)."""
    return {"type": "doc", "version": 1, "content": [_adf_block(b) for b in blocks]}


# --- wiki markup -------------------------------------------------------------------------------


def _wiki_part(part: Part, cloud: bool) -> str:
    match part:
        case str():
            return part
        case Text(text):
            return text
        case Bold(text):
            return f"*{text}*"
        case MentionPart(id_, _):
            return f"[~accountid:{id_}]" if cloud else f"[~{id_}]"
        case Link(text, url):
            return f"[{text}|{url}]"
    raise TypeError(part)


def _wiki_para(value: Para | str, cloud: bool) -> str:
    return "".join(_wiki_part(p, cloud) for p in _para(value).parts)


def _wiki_block(block: Block, cloud: bool) -> str:
    match block:
        case str() | Para():
            return _wiki_para(block, cloud)
        case Quote():
            inner = "\n\n".join(_wiki_para(p, cloud) for p in block.paras)
            return f"{{quote}}\n{inner}\n{{quote}}"
        case Bullets():
            return "\n".join(f"* {_wiki_para(i, cloud)}" for i in block.items)
        case Code(text):
            return f"{{code}}\n{text}\n{{code}}"
        case Panel():
            inner = "\n\n".join(_wiki_para(p, cloud) for p in block.paras)
            return f"{{panel:title=Note}}\n{inner}\n{{panel}}"
        case Table():
            lines = []
            for index, row in enumerate(block.rows):
                sep = "||" if index == 0 else "|"
                lines.append(sep + sep.join(row) + sep)
            return "\n".join(lines)
    raise TypeError(block)


def to_wiki(blocks: Sequence[Block], *, cloud: bool) -> str:
    """The comment as wiki markup; mentions use ``[~accountid:<id>]`` on Cloud, else ``[~<id>]``."""
    return "\n\n".join(_wiki_block(b, cloud) for b in blocks)


__all__ = [
    "Block",
    "Bold",
    "Bullets",
    "Code",
    "Link",
    "MentionPart",
    "Panel",
    "Para",
    "Part",
    "Quote",
    "Table",
    "Text",
    "to_adf",
    "to_wiki",
]
