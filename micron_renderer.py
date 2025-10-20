from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from md_types import (
    AsciiArtPayload,
    AsciiArtPiece,
    BlockEvent,
    BlockKind,
    BlockQuotePayload,
    BlockStyle,
    CodeBlockPayload,
    FrontMatter,
    HeadingPayload,
    ListItemPayload,
    ParagraphPayload,
    StyleSpec,
    StyleUpdateEvent,
)
from plugins import register_renderer


@dataclass
class _StylableBlock:
    index: int
    render: Callable[[BlockStyle], str]
    style: BlockStyle


class MicronRenderer:
    def __init__(self, frontmatter: FrontMatter, *, width: int = 80, **_: Any) -> None:
        self.frontmatter = frontmatter
        self.width = max(1, width)
        self._chunks: List[str] = []
        self._trailing_newlines = 0
        self._last_stylable_block: Optional[_StylableBlock] = None

    def handle_event(self, event: BlockEvent | StyleUpdateEvent) -> None:
        if isinstance(event, BlockEvent):
            self._handle_block_event(event)
        else:
            self._apply_style_update(event.spec)

    def finalize(self) -> str:
        return "".join(self._chunks).rstrip()

    def _handle_block_event(self, event: BlockEvent) -> None:
        if event.kind is BlockKind.PARAGRAPH:
            self._render_paragraph(event.payload, event.style)  # type: ignore[arg-type]
        elif event.kind is BlockKind.HEADING:
            self._render_heading(event.payload, event.style)  # type: ignore[arg-type]
        elif event.kind is BlockKind.CODE_BLOCK:
            self._render_code_block(event.payload, event.style)  # type: ignore[arg-type]
        elif event.kind is BlockKind.BLOCKQUOTE:
            self._render_blockquote(event.payload, event.style)  # type: ignore[arg-type]
        elif event.kind is BlockKind.LIST_ITEM:
            self._render_list_item(event.payload, event.style)  # type: ignore[arg-type]
        elif event.kind is BlockKind.HORIZONTAL_RULE:
            self._render_horizontal_rule(event.style)
        elif event.kind is BlockKind.BLANK_LINE:
            self._ensure_newlines(1)
        elif event.kind is BlockKind.CUSTOM_BLOCK:
            self._render_custom_block(event.payload, event.style)  # type: ignore[arg-type]

    def _render_paragraph(self, payload: ParagraphPayload, style: BlockStyle) -> None:
        content = self._render_inline(payload.text)
        if content:
            def render(target_style: BlockStyle) -> str:
                lines = self._wrap_text(content, target_style)
                return "\n".join(lines)

            block = render(style)
            self._emit_block(block, stylable=True, render_fn=render, style=style)
        self._ensure_newlines(2)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        level = max(1, min(3, payload.level))
        marker = ">" * level + " "
        text = self._render_inline(payload.text)
        if text:
            def render(target_style: BlockStyle) -> str:
                lines = self._wrap_text(text, target_style, initial_prefix=marker)
                return "\n".join(lines)

            block = render(style)
            self._emit_block(block, stylable=True, render_fn=render, style=style)
        self._ensure_newlines(2)

    def _render_code_block(self, payload: CodeBlockPayload, style: BlockStyle) -> None:
        def render(target_style: BlockStyle) -> str:
            margin = " " * max(0, target_style.margin_left)
            fence = margin + "`="
            body_lines = [margin + line.rstrip("\n") for line in payload.lines]
            return "\n".join([fence, *body_lines, fence])

        block = render(style)
        if block:
            self._emit_block(block, stylable=False)
        self._ensure_newlines(2)

    def _render_blockquote(self, payload: BlockQuotePayload, style: BlockStyle) -> None:
        prefix = ">>>>" * max(1, payload.depth)
        text = self._render_inline(payload.text)
        if text:
            def render(target_style: BlockStyle) -> str:
                lines = self._wrap_text(
                    text,
                    target_style,
                    initial_prefix=prefix,
                    subsequent_prefix=prefix,
                    align_blocks=False,
                )
                return "\n".join(lines)

            block = render(style)
            self._emit_block(block, stylable=False)
        self._ensure_newlines(2)

    def _render_list_item(self, payload: ListItemPayload, style: BlockStyle) -> None:
        indent_text = payload.indent.replace("\t", "    ")
        spacing = payload.spacing if payload.spacing else " "
        initial_prefix = f"{indent_text}{payload.marker}{spacing}"
        subsequent_prefix = f"{indent_text}{' ' * len(payload.marker)}{spacing}"
        text = self._render_inline(payload.text)
        if text:
            def render(target_style: BlockStyle) -> str:
                lines = self._wrap_text(
                    text,
                    target_style,
                    initial_prefix=initial_prefix,
                    subsequent_prefix=subsequent_prefix,
                    align_blocks=False,
                )
                return "\n".join(lines)

            block = render(style)
            self._emit_block(block, stylable=False)
        self._ensure_newlines(1)

    def _render_horizontal_rule(self, style: BlockStyle) -> None:
        margin = " " * max(0, style.margin_left)
        self._emit_block(margin + "-", stylable=False)
        self._ensure_newlines(2)

    def _render_custom_block(self, payload: AsciiArtPayload, style: BlockStyle) -> None:
        def render(target_style: BlockStyle) -> str:
            lines = self._layout_ascii_pieces(payload.pieces, target_style)
            return "\n".join(lines)

        block = render(style)
        if block:
            self._emit_block(block, stylable=True, render_fn=render, style=style)
        self._ensure_newlines(2)

    def _render_inline(self, text: str) -> str:
        if not text:
            return ""

        code_segments: List[str] = []
        emphasis_segments: List[str] = []

        def stash_code(match: re.Match[str]) -> str:
            placeholder = f"\u0000CODE{len(code_segments)}\u0000"
            code_segments.append(match.group(1))
            return placeholder

        def stash_emphasis(content: str) -> str:
            placeholder = f"\u0000EMP{len(emphasis_segments)}\u0000"
            emphasis_segments.append(content)
            return placeholder

        text = re.sub(r"`([^`]+)`", stash_code, text)

        text = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: stash_emphasis(f"`!{m.group(1)}`!"),
            text,
        )
        text = re.sub(
            r"__(.+?)__",
            lambda m: stash_emphasis(f"`!{m.group(1)}`!"),
            text,
        )
        text = re.sub(
            r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
            lambda m: stash_emphasis(f"`*{m.group(1)}`*"),
            text,
        )
        text = re.sub(
            r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",
            lambda m: stash_emphasis(f"`*{m.group(1)}`*"),
            text,
        )

        text = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)",
            self._replace_image,
            text,
        )
        text = re.sub(
            r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)",
            self._replace_link,
            text,
        )

        for index, replacement in enumerate(emphasis_segments):
            text = text.replace(f"\u0000EMP{index}\u0000", replacement)
        for index, code in enumerate(code_segments):
            text = text.replace(f"\u0000CODE{index}\u0000", f"`={code}`=")

        return text

    def _wrap_text(
        self,
        text: str,
        style: BlockStyle,
        *,
        initial_prefix: str = "",
        subsequent_prefix: Optional[str] = None,
        align_blocks: bool = True,
    ) -> List[str]:
        if not text:
            return []

        margin_left = max(0, style.margin_left)
        margin_right = max(0, style.margin_right)
        available = max(1, self.width - margin_left - margin_right)
        prefix_first = initial_prefix
        prefix_rest = subsequent_prefix if subsequent_prefix is not None else initial_prefix

        if len(prefix_first) >= available or len(prefix_rest) >= available:
            raw_lines = [f"{prefix_first}{text}".rstrip()]
        else:
            wrapper = textwrap.TextWrapper(
                width=available,
                expand_tabs=False,
                replace_whitespace=True,
                drop_whitespace=True,
                initial_indent=prefix_first,
                subsequent_indent=prefix_rest,
            )
            raw_lines = [line.rstrip() for line in wrapper.wrap(text)]

        margin = " " * margin_left
        if not raw_lines:
            return []

        if align_blocks:
            return [margin + self._apply_alignment(line, style, available) for line in raw_lines]
        return [margin + line for line in raw_lines]

    @staticmethod
    def _apply_alignment(line: str, style: BlockStyle, available: int) -> str:
        align = (style.align or "left").lower()
        if align == "center":
            return line.strip().center(available).rstrip()
        if align == "right":
            return line.strip().rjust(available).rstrip()
        return line.rstrip()

    def _layout_ascii_pieces(self, pieces: List[AsciiArtPiece], style: BlockStyle) -> List[str]:
        if not pieces:
            return []
        if len(pieces) == 1:
            piece = pieces[0]
            return self._align_preformatted_lines(piece.lines, style, piece.align)

        margin_left = min(max(style.margin_left, 0), self.width - 1)
        margin_right = max(style.margin_right, 0)
        available_width = max(1, self.width - margin_left - margin_right)

        heights = [len(piece.lines) for piece in pieces]
        max_height = max(heights) if heights else 0
        widths = [self._ascii_piece_width(piece.lines) for piece in pieces]

        positions = self._compute_ascii_positions(pieces, widths, available_width)
        if positions is None:
            stacked: List[str] = []
            for piece in pieces:
                stacked.extend(self._align_preformatted_lines(piece.lines, style, piece.align))
            return stacked

        canvas = [list(" " * available_width) for _ in range(max_height)]
        for piece, pos, _width in positions:
            for row_index in range(max_height):
                if row_index >= len(piece.lines):
                    continue
                line = piece.lines[row_index].rstrip("\n")
                for col_index, char in enumerate(line):
                    target_index = pos + col_index
                    if target_index >= available_width:
                        break
                    if char != " ":
                        canvas[row_index][target_index] = char

        raw_rows = ["".join(row).rstrip() for row in canvas]
        return self._align_preformatted_lines(raw_rows, style)

    def _ascii_piece_width(self, lines: List[str]) -> int:
        return max((len(line.rstrip("\n")) for line in lines), default=0)

    def _align_preformatted_lines(
        self,
        lines: List[str],
        style: BlockStyle,
        explicit_align: Optional[str] = None,
    ) -> List[str]:
        if not lines:
            return []
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        margin_right = max(style.margin_right, 0)
        available_width = max(1, self.width - margin_left - margin_right)
        processed = [line.rstrip("\n") for line in lines]
        block_width = max((len(line) for line in processed), default=0)
        extra_space = max(0, available_width - block_width)
        align = (explicit_align or style.align or "left").lower()
        if align in {"center", "centre"}:
            align_offset = extra_space // 2
        elif align == "right":
            align_offset = extra_space
        else:
            align_offset = 0
        max_indent = max(0, self.width - block_width)
        indent = min(margin_left + align_offset, max_indent)
        indent_str = " " * indent
        return [indent_str + line for line in processed]

    def _compute_ascii_positions(
        self,
        pieces: List[AsciiArtPiece],
        widths: List[int],
        available_width: int,
    ) -> Optional[List[Tuple[AsciiArtPiece, int, int]]]:
        if available_width <= 0:
            return None

        gap = 4
        left_cursor = 0
        right_cursor = available_width
        placement_map: Dict[int, Tuple[int, int]] = {}

        left_indices: List[int] = []
        center_indices: List[int] = []
        right_indices: List[int] = []

        for index, piece in enumerate(pieces):
            align = (piece.align or "left").lower()
            if align == "right":
                right_indices.append(index)
            elif align == "center":
                center_indices.append(index)
            else:
                left_indices.append(index)

        for index in left_indices:
            width = min(widths[index], available_width)
            pos = left_cursor
            left_cursor = min(available_width, pos + width + gap)
            placement_map[index] = (pos, width)

        for index in reversed(right_indices):
            width = min(widths[index], available_width)
            pos = max(0, right_cursor - width)
            right_cursor = max(0, pos - gap)
            placement_map[index] = (pos, width)

        center_used = False
        for index in center_indices:
            width = min(widths[index], available_width)
            pos = max(0, (available_width - width) // 2)
            if center_used:
                pos = left_cursor
                left_cursor = min(available_width, pos + width + gap)
            else:
                center_used = True
            placement_map[index] = (pos, width)

        for i in range(len(pieces)):
            if i not in placement_map:
                continue
            pos_i, width_i = placement_map[i]
            end_i = pos_i + width_i
            for j in range(i + 1, len(pieces)):
                if j not in placement_map:
                    continue
                pos_j, width_j = placement_map[j]
                if pos_i <= pos_j < end_i or pos_j <= pos_i < pos_j + width_j:
                    return None

        ordered_positions: List[Tuple[AsciiArtPiece, int, int]] = []
        for index, piece in enumerate(pieces):
            if index not in placement_map:
                continue
            pos, width = placement_map[index]
            ordered_positions.append((piece, pos, width))
        return ordered_positions

    def _replace_link(self, match: re.Match[str]) -> str:
        label = match.group(1).strip()
        url = match.group(2).strip()
        if not label:
            return url
        if label == url or url == f"mailto:{label}":
            return f"`[{label}`"
        return f"`[{label}`{url}]"

    def _replace_image(self, match: re.Match[str]) -> str:
        alt = match.group(1).strip()
        url = match.group(2).strip()
        if not alt:
            return f"`[{url}`"
        return f"`[{alt}`{url}]"

    def _write(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        if text.endswith("\n"):
            newline_count = len(text) - len(text.rstrip("\n"))
            prefix = text[:-newline_count]
            if prefix:
                self._trailing_newlines = newline_count
            else:
                self._trailing_newlines += newline_count
        else:
            self._trailing_newlines = 0

    def _ensure_newlines(self, count: int) -> None:
        if self._trailing_newlines < count:
            needed = count - self._trailing_newlines
            self._write("\n" * needed)

    def _emit_block(
        self,
        content: str,
        *,
        stylable: bool,
        render_fn: Optional[Callable[[BlockStyle], str]] = None,
        style: Optional[BlockStyle] = None,
    ) -> None:
        if not content:
            if stylable:
                self._last_stylable_block = None
            return
        index = len(self._chunks)
        self._write(content)
        if stylable and render_fn is not None and style is not None:
            self._last_stylable_block = _StylableBlock(
                index=index,
                render=render_fn,
                style=BlockStyle(
                    align=style.align,
                    margin_left=style.margin_left,
                    margin_right=style.margin_right,
                ),
            )
        else:
            self._last_stylable_block = None

    def _apply_style_update(self, spec: StyleSpec) -> None:
        if self._last_stylable_block is None:
            return
        block = self._last_stylable_block
        new_style = self._combine_styles(block.style, spec)
        updated = block.render(new_style)
        self._chunks[block.index] = updated
        block.style = new_style

    @staticmethod
    def _combine_styles(base: BlockStyle, spec: StyleSpec) -> BlockStyle:
        if spec is None:
            return BlockStyle(
                align=base.align,
                margin_left=base.margin_left,
                margin_right=base.margin_right,
            )
        return BlockStyle(
            align=spec.align or base.align,
            margin_left=spec.margin_left if spec.margin_left is not None else base.margin_left,
            margin_right=spec.margin_right if spec.margin_right is not None else base.margin_right,
        )


def _micron_renderer_factory(*, frontmatter: FrontMatter, **options: Any) -> MicronRenderer:
    return MicronRenderer(frontmatter, **options)


try:
    register_renderer("micron", _micron_renderer_factory)
except ValueError:
    pass
