from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from typing import Any, Callable, List

from ..models import (
    AsciiArtPayload,
    BlockQuotePayload,
    BlockStyle,
    CodeBlockPayload,
    FrontMatter,
    HeadingPayload,
    ListItemPayload,
    ParagraphPayload,
    StyleSpec,
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

LOCAL_SUFFIXES = {".ama"}


class AmaRenderer(TextRenderer):
    def __init__(self, frontmatter: FrontMatter, *, width: int = 78, **_: Any) -> None:
        super().__init__(width=min(width, 78), frontmatter=frontmatter)
        self.links.clear()
        self.link_indices.clear()

    # Block rendering -----------------------------------------------------
    def _render_paragraph(self, payload: ParagraphPayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        def render_fn(target_style: BlockStyle) -> List[str]:
            return self._wrap_and_format(processed, target_style)

        self._emit_block(render_fn(style), stylable=True, render_fn=render_fn, style=style)
        if self.paragraph_spacing > 0:
            self.output.extend([""] * self.paragraph_spacing)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        self._ensure_header_spacing()
        figlet = self._render_figlet_heading(payload.level, payload.text, style)
        if figlet and all(len(line.rstrip()) <= self.width for line in figlet):
            self._emit_block(figlet, stylable=False)
            return
        heading = self._process_inline(payload.text)
        self._emit_block([f"%h {heading}", ""], stylable=False)

    def _render_code_block(self, payload: CodeBlockPayload, style: BlockStyle) -> None:
        margin_left, _, _ = self._margins(style)
        base_indent = " " * margin_left + "   "
        body: List[str] = [base_indent + "`="]
        content_width = max(1, self.width - len(base_indent))
        for raw_line in payload.lines:
            line = raw_line.rstrip("\n")
            segments = self._wrap_code_line_segments(line, content_width) if line else []
            if not segments:
                body.append(base_indent)
                continue
            body.append(base_indent + segments[0])
            for segment in segments[1:]:
                body.append(base_indent + segment)
        body.append(base_indent + "`=")
        self._emit_block(body, stylable=False)

    def _render_blockquote(self, payload: BlockQuotePayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        indent = " " * (3 * max(1, payload.depth))
        def render_fn(target_style: BlockStyle) -> List[str]:
            return self._wrap_and_format(processed, target_style, initial_indent=indent, subsequent_indent=indent)

        self._emit_block(render_fn(style), stylable=True, render_fn=render_fn, style=style)

    def _render_list_item(self, payload: ListItemPayload, style: BlockStyle) -> None:
        base_indent = payload.indent.replace("\t", "    ")
        marker_indent = " " * self.list_marker_indent
        marker = payload.marker
        spacing = " " * self.list_text_spacing
        initial = f"{base_indent}{marker_indent}{marker}{spacing}"
        subsequent = f"{base_indent}{marker_indent}{' ' * len(marker)}{spacing}"
        processed = self._process_inline(payload.text)
        def render_fn(target_style: BlockStyle) -> List[str]:
            return self._wrap_and_format(
                processed,
                target_style,
                initial_indent=initial,
                subsequent_indent=subsequent,
            )

        self._emit_block(render_fn(style), stylable=True, render_fn=render_fn, style=style)

    def _render_horizontal_rule(self, _payload: object, style: BlockStyle) -> None:
        margin_left, _, available = self._margins(style)
        self._emit_block([" " * margin_left + "-" * available], stylable=False)

    def _render_blank_line(self, *_: object) -> None:
        self.output.append("")

    def _render_custom_block(self, payload: AsciiArtPayload, style: BlockStyle) -> None:
        render = partial(self._layout_ascii_pieces, payload.pieces)

        def formatter(target_style: BlockStyle) -> List[str]:
            return render(target_style)

        self._emit_block(formatter(style), stylable=True, render_fn=formatter, style=style)

    # Inline --------------------------------------------------------------
    def _process_inline(self, text: str) -> str:
        code_segments: List[str] = []

        def stash_code(match):
            segment = match.group(0)[1:-1]
            code_segments.append(segment)
            return f"\u0000CODE{len(code_segments) - 1}\u0000"

        text = CODE_STASH_RE.sub(stash_code, text)
        text = text.replace("%", "%%")
        text = STRIKETHROUGH_RE.sub(lambda m: m.group(1), text)
        text = BOLD_RE.sub(self._emphasis_handler("%!", str.upper), text)
        text = ITALIC_RE.sub(self._emphasis_handler("%!", lambda s: s), text)
        text = UNDERLINE_STRONG_RE.sub(self._emphasis_handler("%b", str.upper), text)
        text = UNDERLINE_EM_RE.sub(self._emphasis_handler("%b", lambda s: s), text)

        text = LINK_RE.sub(self._replace_link, text)
        text = IMAGE_RE.sub(self._replace_image, text)

        for idx, code in enumerate(code_segments):
            text = text.replace(f"\u0000CODE{idx}\u0000", f"`={code}`=")
        return text

    @staticmethod
    def _replace_link(match) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        suffix = Path(target).suffix.lower()
        if suffix in LOCAL_SUFFIXES:
            return f"%l{Path(target).name}:{label or target}%t"
        formatted_target = _format_external_url(target)
        if label and label != target:
            return f"{label} ({formatted_target})"
        return formatted_target

    @staticmethod
    def _replace_image(match) -> str:
        alt = match.group(1).strip()
        url = match.group(2).strip()
        suffix = Path(url).suffix.lower()
        if suffix in LOCAL_SUFFIXES:
            return f"%l{Path(url).name}:{alt or url}%t"
        formatted_url = _format_external_url(url)
        if alt and alt != url:
            return f"{alt} ({formatted_url})"
        return formatted_url

    def _emphasis_handler(self, prefix: str, transform: Callable[[str], str]) -> Callable[[Any], str]:
        def handler(match) -> str:
            content = transform(match.group(1))
            return self._apply_emphasis_spacing(match.string, match.start(), match.end(), f"{prefix}{content}%t")

        return handler

    def _wrap_and_format(
        self,
        text: str,
        style: BlockStyle,
        *,
        initial_indent: str = "",
        subsequent_indent: str | None = None,
    ) -> List[str]:
        lines = self._wrap_text(
            text,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent if subsequent_indent is not None else initial_indent,
            style=style,
            hyphenate=self.hyphenate,
        )
        return self._propagate_modes(lines)

    def _propagate_modes(self, lines: List[str]) -> List[str]:
        mode = "%t"
        result: List[str] = []
        for line in lines:
            current_line = line
            if mode in {"%!", "%b"}:
                stripped = current_line.lstrip()
                leading_ws = current_line[: len(current_line) - len(stripped)]
                while stripped.startswith(("%t", "%!", "%b")):
                    stripped = stripped[2:]
                spacer = " " if stripped and not stripped.startswith(" ") else ""
                current_line = f"{leading_ws}{mode}{spacer}{stripped}" if stripped else f"{leading_ws}{mode}"
            end_mode = self._line_end_mode(current_line)
            result.append(current_line)
            mode = end_mode if end_mode in {"%!", "%b"} else "%t"
        return result

    @staticmethod
    def _line_end_mode(line: str) -> str:
        mode = "%t"
        idx = 0
        while True:
            pos = line.find("%", idx)
            if pos == -1 or pos + 1 >= len(line):
                break
            code = line[pos:pos + 2]
            if code in {"%!", "%b", "%t"}:
                mode = code
            elif code == "%%":
                pass
            idx = pos + 2
        return mode


def _ama_renderer_factory(*, frontmatter: FrontMatter, **options: Any) -> AmaRenderer:
    width = int(options.get("width", 78))
    return AmaRenderer(frontmatter, width=width)


try:
    register_renderer("ama", _ama_renderer_factory)
except ValueError:
    pass


def _format_external_url(url: str) -> str:
    stripped = url.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return stripped
    return f"<{stripped}>"
