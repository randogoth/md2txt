from __future__ import annotations

from functools import partial
from typing import Any, List

from md_types import AsciiArtPayload, BlockQuotePayload, BlockStyle, CodeBlockPayload, FrontMatter, HeadingPayload, ListItemPayload, ParagraphPayload
from plugins import register_renderer
from text_renderer import (
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


class MicronRenderer(TextRenderer):
    def __init__(self, frontmatter: FrontMatter, *, width: int = 80, **_: Any) -> None:
        super().__init__(width, frontmatter)
        # Micron format inlines links directly, so suppress link collection overhead.
        self.links.clear()
        self.link_indices.clear()

    def _render_paragraph(self, payload: ParagraphPayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        self._wrap_emit(processed, style, stylable=True, hyphenate=self.hyphenate)
        if self.paragraph_spacing > 0:
            self.output.extend([""] * self.paragraph_spacing)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        self._ensure_header_spacing()
        level = max(1, min(3, payload.level))
        marker = ">" * level
        line = f"{marker} {self._process_inline(payload.text)}".rstrip()
        self._emit_block([line, ""], stylable=False)

    def _render_code_block(self, payload: CodeBlockPayload, style: BlockStyle) -> None:
        margin_left, _, _ = self._margins(style)
        indent = " " * margin_left
        body = [f"{indent}`=", *[f"{indent}{line.rstrip()}" for line in payload.lines], f"{indent}`="]
        self._emit_block(body, stylable=False)
        self.output.append("")

    def _render_blockquote(self, payload: BlockQuotePayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        indent = ">>>>" * max(1, payload.depth)
        self._wrap_emit(processed, style, initial_indent=indent, subsequent_indent=indent, hyphenate=self.hyphenate)
        self.output.append("")

    def _render_list_item(self, payload: ListItemPayload, style: BlockStyle) -> None:
        base_indent = payload.indent.replace("\t", "    ")
        marker_indent = " " * self.list_marker_indent
        marker = payload.marker
        spacing = " " * self.list_text_spacing
        initial = f"{base_indent}{marker_indent}{marker}{spacing}"
        subsequent = f"{base_indent}{marker_indent}{' ' * len(marker)}{spacing}"
        processed = self._process_inline(payload.text)
        self._wrap_emit(processed, style, initial_indent=initial, subsequent_indent=subsequent, hyphenate=self.hyphenate)

    def _render_custom_block(self, payload: AsciiArtPayload, style: BlockStyle) -> None:
        render = partial(self._layout_ascii_pieces, payload.pieces)
        self._emit_block(render(style), stylable=True, render_fn=render, style=style)

    def _render_horizontal_rule(self, _payload: object, style: BlockStyle) -> None:
        margin_left, _, available = self._margins(style)
        self._emit_block([" " * margin_left + "-" * available], stylable=False)

    # Inline transformations -------------------------------------------------
    def _process_inline(self, text: str) -> str:
        code_segments: List[str] = []

        def stash_code(match):
            segment = match.group(0)
            code_segments.append(segment[1:-1])
            return f"\u0000CODE{len(code_segments) - 1}\u0000"

        text = CODE_STASH_RE.sub(stash_code, text)
        text = STRIKETHROUGH_RE.sub(lambda m: f"~~{m.group(1)}~~", text)  # keep strikethrough literal for now
        text = BOLD_RE.sub(lambda m: self._apply_emphasis_spacing(m.string, m.start(), m.end(), f"`!{m.group(1)}`!"), text)
        text = ITALIC_RE.sub(lambda m: self._apply_emphasis_spacing(m.string, m.start(), m.end(), f"`*{m.group(1)}`*"), text)
        text = UNDERLINE_STRONG_RE.sub(lambda m: self._apply_emphasis_spacing(m.string, m.start(), m.end(), f"`!{m.group(1)}`!"), text)
        text = UNDERLINE_EM_RE.sub(lambda m: self._apply_emphasis_spacing(m.string, m.start(), m.end(), f"`*{m.group(1)}`*"), text)
        text = LINK_RE.sub(self._replace_link, text)
        text = IMAGE_RE.sub(self._replace_image, text)

        for idx, code in enumerate(code_segments):
            text = text.replace(f"\u0000CODE{idx}\u0000", f"`={code}`=")
        return text

    @staticmethod
    def _replace_link(match) -> str:
        label = match.group(1).strip()
        url = match.group(2).strip()
        if not label:
            return url
        if label == url or url == f"mailto:{label}":
            return f"`[{label}`"
        return f"`[{label}`{url}]"

    @staticmethod
    def _replace_image(match) -> str:
        alt = match.group(1).strip()
        url = match.group(2).strip()
        return f"`[{alt or url}`{url}]"


def _micron_renderer_factory(*, frontmatter: FrontMatter, **options: Any) -> MicronRenderer:
    width = int(options.get("width", 80))
    return MicronRenderer(frontmatter, width=width)


try:
    register_renderer("micron", _micron_renderer_factory)
except ValueError:
    pass
