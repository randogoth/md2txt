from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class BlockKind(Enum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    CODE_BLOCK = "code_block"
    BLOCKQUOTE = "blockquote"
    LIST_ITEM = "list_item"
    HORIZONTAL_RULE = "horizontal_rule"
    BLANK_LINE = "blank_line"


@dataclass
class BlockStyle:
    align: str = "left"
    margin_left: int = 0
    margin_right: int = 0


@dataclass
class StyleSpec:
    align: Optional[str] = None
    margin_left: Optional[int] = None
    margin_right: Optional[int] = None


@dataclass
class FrontMatter:
    h1_font: str = "small"
    h2_font: str = "caps"
    h3_font: str = "title"
    margin_left: int = 2
    margin_right: int = 2
    paragraph_spacing: int = 2
    hyphenate: bool = False
    hyphen_lang: str = "en_US"
    figlet_fallback: bool = False
    header_spacing: int = 2


@dataclass
class ParagraphPayload:
    text: str


@dataclass
class HeadingPayload:
    level: int
    text: str


@dataclass
class CodeBlockPayload:
    lines: List[str]


@dataclass
class BlockQuotePayload:
    depth: int
    text: str


@dataclass
class ListItemPayload:
    indent: str
    marker: str
    spacing: str
    text: str
    ordered: bool


@dataclass
class BlockEvent:
    kind: BlockKind
    payload: object
    style: BlockStyle
    stylable: bool = False


@dataclass
class StyleUpdateEvent:
    spec: StyleSpec
