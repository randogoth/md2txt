from __future__ import annotations

import re
import textwrap
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AsciiArtPayload,
    BlockEvent,
    BlockKind,
    BlockQuotePayload,
    BlockStyle,
    CodeBlockPayload,
    FrontMatter,
    HeadingPayload,
    ListItemPayload,
    ParagraphPayload,
    StyleUpdateEvent,
)
from ..plugins import register_renderer
from .text import (
    BOLD_RE,
    CODE_STASH_RE,
    IMAGE_RE,
    ITALIC_RE,
    LINK_RE,
    STRIKETHROUGH_RE,
    TextRenderer,
    UNDERLINE_EM_RE,
    UNDERLINE_STRONG_RE,
)


class GeminiRenderer:
    """Render Markdown events as Gemtext suitable for the Gemini protocol."""

    def __init__(self, frontmatter: FrontMatter, *, width: int = 80, preformatted_alt: str | None = None, **_: Any) -> None:
        self.frontmatter = frontmatter
        self._text_renderer = TextRenderer(width=width, frontmatter=frontmatter)
        self.width = max(20, width)
        self.preformatted_alt = preformatted_alt or ""
        self.output: List[str] = []
        self._in_list: bool = False
        self.links_per_block = frontmatter.links_per_block
        self._link_indices: Dict[str, int] = {}
        self._link_catalog: Dict[int, Tuple[str, Optional[str]]] = {}
        self._pending_links: List[int] = []

    def handle_event(self, event: BlockEvent | StyleUpdateEvent) -> None:
        if isinstance(event, StyleUpdateEvent):
            # Gemtext has no notion of per-block styling; ignore updates.
            return
        handler = {
            BlockKind.PARAGRAPH: self._render_paragraph,
            BlockKind.HEADING: self._render_heading,
            BlockKind.CODE_BLOCK: self._render_code_block,
            BlockKind.BLOCKQUOTE: self._render_blockquote,
            BlockKind.LIST_ITEM: self._render_list_item,
            BlockKind.HORIZONTAL_RULE: self._render_horizontal_rule,
            BlockKind.BLANK_LINE: self._render_blank_line,
            BlockKind.CUSTOM_BLOCK: self._render_custom_block,
        }.get(event.kind)
        if handler is not None:
            handler(event.payload, event.style)

    def finalize(self) -> List[str]:
        self._end_list()
        self._flush_pending_links(trailing_blank=False)
        return self.output

    # Rendering helpers -------------------------------------------------
    def _render_paragraph(self, payload: ParagraphPayload, _style: BlockStyle) -> None:
        self._end_list()
        text, links = self._process_inline(payload.text)
        if text:
            self._ensure_blank_line()
            self.output.extend(self._wrap_text(text))
        self._handle_links(links)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        self._end_list()
        text, links = self._process_inline(payload.text)

        def render_heading(target_style: BlockStyle) -> List[str]:
            figlet = self._text_renderer._render_figlet_heading(payload.level, text, target_style)
            if figlet:
                banner = [line.rstrip() for line in figlet]
                fence_header = self._preformatted_header()
                lines = [fence_header, *banner, "```"]
                return lines

            level = max(1, min(3, payload.level))
            heading = text
            if not heading:
                return []
            marker = "#" * level
            wrapped = self._wrap_text(
                heading,
                initial=f"{marker} ",
                subsequent=f"{marker} ",
            )
            if not wrapped or wrapped[-1] != "":
                wrapped.append("")
            return wrapped

        lines = render_heading(style)
        if not lines:
            return
        self._ensure_blank_line()
        self.output.extend(lines)
        self._handle_links(links)

    def _render_code_block(self, payload: CodeBlockPayload, _style: BlockStyle) -> None:
        self._end_list()
        self._ensure_blank_line()
        self.output.append(self._preformatted_header())
        for line in payload.lines:
            self.output.append(line.rstrip("\n"))
        self.output.append("```")

    def _render_blockquote(self, payload: BlockQuotePayload, _style: BlockStyle) -> None:
        self._end_list()
        text, links = self._process_inline(payload.text)
        if text:
            self._ensure_blank_line()
            wrapped = self._wrap_text(text, initial="> ", subsequent="> ")
            self.output.extend(wrapped)
        self._handle_links(links)

    def _render_list_item(self, payload: ListItemPayload, _style: BlockStyle) -> None:
        indent = payload.indent.replace("\t", "    ")
        depth = max(0, len(indent) // 2)
        bullet_prefix = "  " * depth + "* "
        continuation = "  " * depth + "  "
        text, links = self._process_inline(payload.text)
        self._begin_list()
        if text:
            self.output.extend(self._wrap_text(text, initial=bullet_prefix, subsequent=continuation))
        self._handle_links(links, leading_blank=False, trailing_blank=False)

    def _render_horizontal_rule(self, _payload: object, _style: BlockStyle) -> None:
        self._end_list()
        self._ensure_blank_line()
        self.output.append("---")

    def _render_blank_line(self, *_: object) -> None:
        self._end_list()
        self._ensure_blank_line(force=True)

    def _render_custom_block(self, payload: AsciiArtPayload, _style: BlockStyle) -> None:
        lines: List[str] = []
        for index, piece in enumerate(payload.pieces):
            lines.extend(line.rstrip("\n") for line in piece.lines)
            if index + 1 < len(payload.pieces):
                lines.append("")
        if not lines:
            return
        self._end_list()
        self._ensure_blank_line()
        self.output.append(self._preformatted_header())
        self.output.extend(lines)
        self.output.append("```")

    # Internal utilities ------------------------------------------------
    def _begin_list(self) -> None:
        if not self._in_list:
            self._ensure_blank_line()
            self._in_list = True

    def _end_list(self) -> None:
        if self._in_list:
            if self.output and self.output[-1] != "":
                self.output.append("")
            self._in_list = False

    def _ensure_blank_line(self, *, force: bool = False) -> None:
        if not self.output:
            return
        if force or self.output[-1] != "":
            self.output.append("")

    def _wrap_text(
        self,
        text: str,
        *,
        initial: str = "",
        subsequent: Optional[str] = None,
    ) -> List[str]:
        normalized = self._normalise_whitespace(text)
        if not normalized:
            return [initial.rstrip()] if initial else []
        wrapper = textwrap.TextWrapper(
            width=self.width,
            expand_tabs=False,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=False,
            break_on_hyphens=True,
            initial_indent=initial,
            subsequent_indent=subsequent if subsequent is not None else initial,
        )
        return [line.rstrip() for line in wrapper.wrap(normalized)]

    def _normalise_whitespace(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _register_link(self, url: str, label: Optional[str]) -> int:
        key = url
        index = self._link_indices.get(key)
        if index is None:
            index = len(self._link_indices) + 1
            self._link_indices[key] = index
            self._link_catalog[index] = (url, label)
        else:
            stored_url, stored_label = self._link_catalog.get(index, (url, None))
            if label and not stored_label:
                self._link_catalog[index] = (stored_url, label)
        return index

    def _process_inline(self, text: str) -> Tuple[str, List[int]]:
        code_segments: List[str] = []
        indices: List[int] = []

        def stash_code(match):
            segment = match.group(0)[1:-1]
            code_segments.append(segment)
            return f"\u0000CODE{len(code_segments) - 1}\u0000"

        def register_link(label: str, target: str) -> str:
            url = target.strip()
            label_clean = label.strip()
            display = label_clean or url
            link_label = label_clean if label_clean and label_clean != url else None
            index = self._register_link(url, link_label)
            if index not in indices:
                indices.append(index)
            return f"{display} [{index}]"

        text = CODE_STASH_RE.sub(stash_code, text)
        text = STRIKETHROUGH_RE.sub(lambda m: m.group(1), text)
        text = BOLD_RE.sub(lambda m: m.group(1), text)
        text = ITALIC_RE.sub(lambda m: m.group(1), text)
        text = UNDERLINE_STRONG_RE.sub(lambda m: m.group(1), text)
        text = UNDERLINE_EM_RE.sub(lambda m: m.group(1), text)
        text = LINK_RE.sub(lambda m: register_link(m.group(1), m.group(2)), text)
        text = IMAGE_RE.sub(lambda m: register_link(m.group(1), m.group(2)), text)

        for idx, code in enumerate(code_segments):
            text = text.replace(f"\u0000CODE{idx}\u0000", code)

        return self._normalise_whitespace(text), indices

    def _handle_links(
        self,
        indices: List[int],
        *,
        leading_blank: bool = True,
        trailing_blank: bool = True,
    ) -> None:
        if not indices:
            return
        if self.links_per_block:
            self._emit_links(indices, leading_blank=leading_blank, trailing_blank=trailing_blank)
        else:
            for index in indices:
                if index not in self._pending_links:
                    self._pending_links.append(index)

    def _flush_pending_links(self, *, trailing_blank: bool) -> None:
        if not self._pending_links:
            return
        self._emit_links(self._pending_links, leading_blank=True, trailing_blank=trailing_blank)
        self._pending_links.clear()

    def _emit_links(
        self,
        indices: List[int],
        *,
        leading_blank: bool = True,
        trailing_blank: bool = True,
    ) -> None:
        if not indices:
            return
        trailing_blanks: List[str] = []
        while self.output and self.output[-1] == "":
            trailing_blanks.append(self.output.pop())
        trailing_blanks.reverse()
        if leading_blank and self.output and self.output[-1] != "":
            self.output.append("")
        for index in indices:
            url, label = self._link_catalog.get(index, ("", None))
            label_text = f"[{index}]"
            if label:
                label_text = f"{label_text} {label}"
            line = f"=> {url}" if not label_text else f"=> {url} {label_text}"
            self.output.append(line)
        if trailing_blank and not trailing_blanks and self.output and self.output[-1] != "":
            self.output.append("")
        if trailing_blanks:
            self.output.extend(trailing_blanks)

    def _preformatted_header(self) -> str:
        if self.preformatted_alt:
            return f"``` {self.preformatted_alt}"
        return "```"


def _gemini_renderer_factory(*, frontmatter: FrontMatter, **options: Any) -> GeminiRenderer:
    width = int(options.get("width", 80))
    alt = options.get("preformatted_alt")
    return GeminiRenderer(frontmatter, width=width, preformatted_alt=alt)


try:
    register_renderer("gemini", _gemini_renderer_factory)
except ValueError:
    pass
