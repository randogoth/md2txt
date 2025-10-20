from __future__ import annotations

import re
import string
import textwrap
from dataclasses import dataclass
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple

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

try:  # pragma: no cover - optional dependency
    from hyphen import Hyphenator as _Hyphenator  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    _Hyphenator = None  # type: ignore[misc]

if _Hyphenator is not None:  # pragma: no branch
    Hyphenator = _Hyphenator  # type: ignore[assignment]
else:  # pragma: no cover - fallback path
    try:
        import pyphen
    except ImportError:  # pragma: no cover - handled at runtime
        Hyphenator = None  # type: ignore[misc, assignment]
    else:
        class _PyphenWrapper:
            def __init__(self, lang: str) -> None:
                self._dic = pyphen.Pyphen(lang=lang)

            def hyphenate_word(self, word: str):  # type: ignore[override]
                inserted = self._dic.inserted(word)
                if not inserted:
                    return []
                return inserted.split("-")

        Hyphenator = _PyphenWrapper  # type: ignore[assignment]

try:
    from pyfiglet import Figlet, FontNotFound
except ImportError:  # pragma: no cover - emit a helpful error at runtime instead
    Figlet = None  # type: ignore[assignment]
    FontNotFound = ValueError  # type: ignore[assignment]


CODE_STASH_RE = re.compile(r"`[^`]*`")
STRIKETHROUGH_RE = re.compile(r"~~(.*?)~~")
BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)")
UNDERLINE_STRONG_RE = re.compile(r"__(.*?)__")
UNDERLINE_EM_RE = re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")


@dataclass
class BlockRecord:
    start: int
    length: int
    render: Callable[[BlockStyle], List[str]]
    style: BlockStyle


class TextRenderer:
    def __init__(self, width: int, frontmatter: FrontMatter) -> None:
        self.width = width
        self.frontmatter = frontmatter
        self.links: List[tuple[int, str]] = []
        self.link_indices: Dict[str, int] = {}
        self.figlets: Dict[tuple, Figlet] = {}
        self.output: List[str] = []
        self.paragraph_spacing = max(0, frontmatter.paragraph_spacing)
        self.hyphenate = frontmatter.hyphenate
        self.hyphen_lang = frontmatter.hyphen_lang or "en_US"
        self.figlet_fallback = frontmatter.figlet_fallback
        self.header_spacing = max(0, frontmatter.header_spacing)
        self.wrap_code_blocks = frontmatter.wrap_code_blocks
        self.code_block_line_numbers = frontmatter.code_block_line_numbers
        self.blockquote_bars = frontmatter.blockquote_bars
        self.wrap_code_blocks_indent = (
            max(0, frontmatter.code_block_wrap_indent) if self.wrap_code_blocks else 0
        )
        self.list_marker_indent = max(0, frontmatter.list_marker_indent)
        self.list_text_spacing = max(0, frontmatter.list_text_spacing)
        self._base_style = BlockStyle(
            align="left",
            margin_left=max(0, frontmatter.margin_left),
            margin_right=max(0, frontmatter.margin_right),
        )
        self._last_stylable_block: Optional[BlockRecord] = None
        self.hyphenator: Optional[Hyphenator]
        if self.hyphenate:
            if Hyphenator is None:
                raise RuntimeError("PyHyphen is required for hyphenation but is not installed.")
            try:
                self.hyphenator = Hyphenator(self.hyphen_lang)
            except Exception as exc:  # pragma: no cover - defensive
                raise RuntimeError(f"Failed to initialise hyphenator for language '{self.hyphen_lang}': {exc}") from exc
        else:
            self.hyphenator = None
        self._handlers: Dict[BlockKind, Callable[[object, BlockStyle], None]] = {
            BlockKind.PARAGRAPH: self._render_paragraph,
            BlockKind.HEADING: self._render_heading,
            BlockKind.CODE_BLOCK: self._render_code_block,
            BlockKind.BLOCKQUOTE: self._render_blockquote,
            BlockKind.LIST_ITEM: self._render_list_item,
            BlockKind.HORIZONTAL_RULE: self._render_horizontal_rule,
            BlockKind.BLANK_LINE: self._render_blank_line,
            BlockKind.CUSTOM_BLOCK: self._render_custom_block,
        }

    def handle_event(self, event: BlockEvent | StyleUpdateEvent) -> None:
        if isinstance(event, BlockEvent):
            self._handle_block_event(event)
        else:
            self._apply_style_to_last_block(event.spec)

    def finalize(self) -> List[str]:
        if self.links:
            if self.output and self.output[-1] != "":
                self.output.append("")
            for index, url in self.links:
                entry = f"[{index}] {url}"
                self.output.extend(
                    self._wrap_text(
                        entry,
                        initial_indent="",
                        subsequent_indent="",
                        style=self._base_style,
                    )
                )
            self._last_stylable_block = None
        return self.output

    def _handle_block_event(self, event: BlockEvent) -> None:
        handler = self._handlers.get(event.kind)
        if handler:
            handler(event.payload, event.style)
        else:
            self._last_stylable_block = None

    def _render_paragraph(self, payload: ParagraphPayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        self._wrap_emit(processed, style, stylable=True, hyphenate=self.hyphenate)
        if self.paragraph_spacing > 0:
            self.output.extend([""] * self.paragraph_spacing)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        self._ensure_header_spacing()

        self._emit_render(partial(self._render_heading_lines, payload.level, payload.text), style, stylable=True)

    def _render_code_block(self, payload: CodeBlockPayload, style: BlockStyle) -> None:
        self._emit_render(partial(self._format_code_block, payload.lines), style)

    def _render_blockquote(self, payload: BlockQuotePayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        indent_unit = " | " if self.blockquote_bars else "   "
        indent = indent_unit * max(1, payload.depth)
        self._wrap_emit(
            processed,
            style,
            initial_indent=indent,
            subsequent_indent=indent,
            hyphenate=self.hyphenate,
        )

    def _render_list_item(self, payload: ListItemPayload, style: BlockStyle) -> None:
        base_indent = payload.indent.replace("\t", "    ")
        marker_indent = " " * self.list_marker_indent
        marker = payload.marker
        text_spacing = " " * self.list_text_spacing
        initial_indent = f"{base_indent}{marker_indent}{marker}{text_spacing}"
        subsequent_indent = f"{base_indent}{marker_indent}{' ' * len(marker)}{text_spacing}"
        processed = self._process_inline(payload.text)
        self._wrap_emit(
            processed,
            style,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
            hyphenate=self.hyphenate,
        )

    def _render_horizontal_rule(self, _payload: object, style: BlockStyle) -> None:
        line = self._render_horizontal_rule_line(style)
        self._emit_block([line], stylable=False)

    def _render_blank_line(self, *_: object) -> None:
        if self.paragraph_spacing == 0:
            self.output.append("")

    def _render_custom_block(self, payload: AsciiArtPayload, style: BlockStyle) -> None:

        self._emit_render(partial(self._layout_ascii_pieces, payload.pieces), style, stylable=True)

    def _layout_ascii_pieces(self, pieces: List[AsciiArtPiece], style: BlockStyle) -> List[str]:
        if not pieces:
            return []
        if len(pieces) == 1:
            piece = pieces[0]
            return self._align_preformatted_lines(piece.lines, style, piece.align)

        margin_left, _, available_width = self._margins(style)

        heights = [len(piece.lines) for piece in pieces]
        max_height = max(heights) if heights else 0
        widths = [self._ascii_piece_width(piece.lines) for piece in pieces]

        positions = self._compute_ascii_positions(pieces, widths, available_width)
        if positions is None:
            return [
                aligned_line
                for piece in pieces
                for aligned_line in self._align_preformatted_lines(piece.lines, style, piece.align)
            ]

        canvas = [list(" " * available_width) for _ in range(max_height)]
        for piece, pos, width in positions:
            lines = piece.lines
            for row_index in range(max_height):
                if row_index >= len(lines):
                    continue
                line = lines[row_index]
                for col_index, char in enumerate(line):
                    target_index = pos + col_index
                    if target_index >= available_width:
                        break
                    if char == " ":
                        continue
                    canvas[row_index][target_index] = char

        prefix = " " * margin_left
        return [prefix + "".join(row).rstrip() for row in canvas]

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

        groups: Dict[str, List[int]] = {"left": [], "center": [], "right": []}
        for index, piece in enumerate(pieces):
            groups.get((piece.align or "left").lower(), groups["left"]).append(index)

        for index in groups["left"]:
            width = min(widths[index], available_width)
            pos = left_cursor
            left_cursor = min(available_width, pos + width + gap)
            placement_map[index] = (pos, width)

        for index in reversed(groups["right"]):
            width = min(widths[index], available_width)
            pos = max(0, right_cursor - width)
            right_cursor = max(0, pos - gap)
            placement_map[index] = (pos, width)

        center_used = False
        for index in groups["center"]:
            width = min(widths[index], available_width)
            pos = max(0, (available_width - width) // 2)
            if center_used:
                # shift to current left cursor if already used center
                pos = left_cursor
                left_cursor = min(available_width, pos + width + gap)
            else:
                center_used = True
            placement_map[index] = (pos, width)

        # Detect overlap; if found, abort to fallback
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
        margin_left, _, available_width = self._margins(style)
        processed = [line.rstrip("\n") for line in lines]
        block_width = max((len(line) for line in processed), default=0)
        extra_space = max(0, available_width - block_width)
        align = (explicit_align or style.align or "left").lower()
        if align == "center" or align == "centre":
            align_offset = extra_space // 2
        elif align == "right":
            align_offset = extra_space
        else:
            align_offset = 0
        max_indent = max(0, self.width - block_width)
        indent = min(margin_left + align_offset, max_indent)
        indent_str = " " * indent
        return [indent_str + line for line in processed]

    def _margins(self, style: BlockStyle) -> Tuple[int, int, int]:
        margin_left = max(0, min(style.margin_left, self.width - 1))
        remaining = self.width - margin_left
        margin_right = max(0, min(style.margin_right, max(0, remaining - 1)))
        available = max(1, self.width - margin_left - margin_right)
        return margin_left, margin_right, available

    def _wrap_emit(
        self,
        text: str,
        style: BlockStyle,
        *,
        initial_indent: str = "",
        subsequent_indent: Optional[str] = None,
        hyphenate: bool,
        stylable: bool = False,
    ) -> None:
        if not text:
            return

        self._emit_render(
            lambda target_style: self._wrap_text(
                text,
                initial_indent=initial_indent,
                subsequent_indent=subsequent_indent if subsequent_indent is not None else initial_indent,
                style=target_style,
                hyphenate=hyphenate,
            ),
            style,
            stylable=stylable,
        )

    def _emit_render(
        self,
        render_fn: Callable[[BlockStyle], List[str]],
        style: BlockStyle,
        *,
        stylable: bool = False,
    ) -> None:
        self._emit_block(
            render_fn(style),
            stylable=stylable,
            render_fn=render_fn if stylable else None,
            style=style if stylable else None,
        )

    def _emit_block(
        self,
        lines: List[str],
        *,
        stylable: bool,
        render_fn: Optional[Callable[[BlockStyle], List[str]]] = None,
        style: Optional[BlockStyle] = None,
    ) -> None:
        if not lines:
            return
        start = len(self.output)
        self.output.extend(lines)
        if stylable and render_fn is not None and style is not None:
            self._last_stylable_block = BlockRecord(start, len(lines), render_fn, style)
        else:
            self._last_stylable_block = None

    def _apply_style_to_last_block(self, spec: StyleSpec) -> None:
        if self._last_stylable_block is None:
            return
        new_style = self._combine_styles(self._last_stylable_block.style, spec)
        new_lines = self._last_stylable_block.render(new_style)
        start = self._last_stylable_block.start
        end = start + self._last_stylable_block.length
        self.output[start:end] = new_lines
        self._last_stylable_block.length = len(new_lines)
        self._last_stylable_block.style = new_style

    def _combine_styles(self, base: BlockStyle, spec: Optional[StyleSpec]) -> BlockStyle:
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

    def _render_heading_lines(self, level: int, text: str, style: BlockStyle) -> List[str]:
        font_name = getattr(self.frontmatter, f"h{level}_font", "standard")
        style_key = font_name.lower()
        if style_key in {"caps", "title"}:
            return self._render_h4_plus(text, style, transform=style_key)
        if level <= 3:
            figlet_lines = self._render_figlet_heading(level, text, style)
            if figlet_lines is not None:
                return figlet_lines + [""]
        return self._render_h4_plus(text, style)

    def _render_figlet_heading(self, level: int, text: str, style: BlockStyle) -> Optional[List[str]]:
        if Figlet is None:
            return None
        font_name = getattr(self.frontmatter, f"h{level}_font", "standard")
        cache_key = (font_name, "wide")
        figlet = self.figlets.get(cache_key)
        if figlet is None:
            try:
                figlet = Figlet(font=font_name, width=max(self.width, 100000))
            except (FontNotFound, TypeError):
                return None
            self.figlets[cache_key] = figlet

        if not text.split():
            return []

        available_width = self._effective_width(style)
        justify = self._figlet_justify(style.align)

        render_key = (font_name, available_width, justify)
        render_figlet = self.figlets.get(render_key)
        if render_figlet is None:
            try:
                render_figlet = Figlet(font=font_name, width=available_width, justify=justify)
            except (FontNotFound, TypeError):
                return None
            self.figlets[render_key] = render_figlet

        rendered = render_figlet.renderText(text).rstrip("\n").splitlines()
        if not rendered:
            return []

        lines = [line.rstrip("\n") for line in rendered]
        overflow_detected = any(len(line.rstrip()) > available_width for line in lines)
        if overflow_detected and self.figlet_fallback:
            return None

        margin_left, _, _ = self._margins(style)
        indent_str = " " * margin_left
        return [indent_str + line.rstrip() for line in lines]

    def _figlet_justify(self, align: str) -> str:
        if align == "center":
            return "center"
        if align == "right":
            return "right"
        return "left"

    def _render_h4_plus(self, text: str, style: BlockStyle, transform: str = "caps") -> List[str]:
        if transform == "title":
            processed = self._to_title_case(text)
        elif transform == "caps":
            processed = text.upper()
        else:
            processed = text
        wrapped = self._wrap_text(processed, style=style)
        output: List[str] = []
        for line in wrapped:
            line = line.rstrip()
            if not line.strip():
                output.append("")
                continue
            leading = len(line) - len(line.lstrip(" "))
            underline = " " * leading + "-" * len(line.lstrip(" "))
            output.append(line)
            output.append(underline)
        output.append("")
        return output

    def _to_title_case(self, value: str) -> str:
        return string.capwords(value)

    def _render_horizontal_rule_line(self, style: BlockStyle) -> str:

        margin_left, _, available_width = self._margins(style)
        return " " * margin_left + "-" * available_width

    def _format_code_block(self, lines: List[str], style: BlockStyle) -> List[str]:
        if not lines:
            return []
        margin_left, _, available = self._margins(style)
        indent = " " * margin_left
        numbered = self.code_block_line_numbers
        wrapped = self.wrap_code_blocks

        if numbered:
            width = max(2, len(str(len(lines))))
            lead_fmt = f"{indent}{{:0{width}d}} | "
            cont_prefix = f"{indent}{' ' * width} | "
            content_width = max(1, available - (len(lead_fmt.format(0)) - len(indent)))
        else:
            base = indent + "   "
            lead_fmt = None
            cont_prefix = base
            content_width = max(1, available - 3)

        def lead(idx: int) -> str:
            return lead_fmt.format(idx) if numbered else cont_prefix

        formatted: List[str] = []
        for idx, line in enumerate(lines, start=1):
            prefix = lead(idx)
            segments = (
                self._wrap_code_line_segments(line, content_width)
                if wrapped and line
                else ([line] if line else [])
            )
            if not segments:
                formatted.append(prefix)
                continue
            formatted.append(prefix + segments[0])
            formatted.extend(cont_prefix + segment for segment in segments[1:])
        formatted.append(indent if indent else "")
        return formatted

    def _wrap_code_line_segments(self, line: str, content_width: int) -> List[str]:
        if not line:
            return []
        segments: List[str] = []
        remaining = line
        current_indent = 0
        max_indent_allowed = max(0, content_width - 1)
        while remaining:
            available = max(1, content_width - current_indent)
            segment, remaining = self._split_code_segment(remaining, available)
            segments.append(" " * current_indent + segment)
            leading = self._leading_space_count(segment)
            total_indent = current_indent + leading
            desired_indent = total_indent + self.wrap_code_blocks_indent
            current_indent = min(max_indent_allowed, desired_indent)
        return segments

    def _split_code_segment(self, text: str, max_width: int) -> tuple[str, str]:
        if max_width <= 0:
            max_width = 1
        if len(text) <= max_width:
            return text, ""
        slice_text = text[:max_width]
        break_pos = -1
        for idx, char in enumerate(slice_text):
            if char in {" ", "\t"}:
                break_pos = idx
        if break_pos <= 0:
            segment = slice_text
            remainder = text[len(segment) :]
        else:
            segment = text[:break_pos]
            remainder = text[break_pos:]
        if not segment:
            segment = slice_text
            remainder = text[len(segment) :]
        return segment, remainder

    def _leading_space_count(self, text: str) -> int:
        count = 0
        for char in text:
            if char == " ":
                count += 1
            elif char == "\t":
                count += 1
            else:
                break
        return count

    def _process_inline(self, text: str) -> str:
        code_segments: List[str] = []

        def stash_code(match: re.Match[str]) -> str:
            code_segments.append(match.group(0))
            return f"\u0000CODE{len(code_segments) - 1}\u0000"

        text = CODE_STASH_RE.sub(stash_code, text)

        text = self._apply_pattern(text, STRIKETHROUGH_RE, lambda m, src: self._stylize_delimited(m.group(1), "-", transform="preserve"))
        text = self._apply_pattern(text, BOLD_RE, lambda m, src: self._replace_spaced_emphasis(src, m, transform="upper"))
        text = self._apply_pattern(text, ITALIC_RE, lambda m, src: self._replace_spaced_emphasis(src, m, transform="preserve"))

        emphasis_segments: List[str] = []

        def apply_with_placeholder(
            current: str,
            pattern: re.Pattern[str],
            handler: Callable[[re.Match[str]], str],
        ) -> str:
            def replacer(match: re.Match[str]) -> str:
                replacement = handler(match)
                placeholder = f"\u0000EMP{len(emphasis_segments)}\u0000"
                emphasis_segments.append(replacement)
                return placeholder

            return pattern.sub(replacer, current)

        text = apply_with_placeholder(
            text,
            UNDERLINE_STRONG_RE,
            lambda m: self._apply_emphasis_spacing(
                m.string,
                m.start(),
                m.end(),
                self._stylize_delimited(m.group(1), "_", transform="upper", word_repeat=3),
            ),
        )
        text = apply_with_placeholder(
            text,
            UNDERLINE_EM_RE,
            lambda m: self._apply_emphasis_spacing(
                m.string,
                m.start(),
                m.end(),
                self._stylize_delimited(m.group(1), "_", transform="preserve", word_repeat=3),
            ),
        )

        text = LINK_RE.sub(self._handle_link, text)
        text = IMAGE_RE.sub(self._handle_image, text)

        for index, replacement in enumerate(emphasis_segments):
            placeholder = f"\u0000EMP{index}\u0000"
            text = text.replace(placeholder, replacement)

        for index, code in enumerate(code_segments):
            placeholder = f"\u0000CODE{index}\u0000"
            text = text.replace(placeholder, code)

        return text

    def _apply_pattern(
        self,
        text: str,
        pattern: re.Pattern[str],
        handler: Callable[[re.Match[str], str], str],
    ) -> str:
        result: List[str] = []
        last = 0
        for match in pattern.finditer(text):
            result.append(text[last:match.start()])
            replacement = handler(match, text)
            result.append(replacement)
            last = match.end()
        result.append(text[last:])
        return "".join(result)

    def _replace_spaced_emphasis(
        self,
        source: str,
        match: re.Match[str],
        *,
        transform: str,
    ) -> str:
        stylized = self._stylize_letters(match.group(1), transform=transform)
        if not stylized:
            return stylized
        return self._apply_emphasis_spacing(source, match.start(), match.end(), stylized)

    def _apply_emphasis_spacing(self, source: str, start: int, end: int, stylized: str) -> str:
        if not stylized:
            return stylized
        prefix = ""
        suffix = ""
        if start > 0:
            before_char = source[start - 1]
            if before_char.isalnum():
                prefix = "  "
        if end < len(source):
            after_char = source[end]
            if after_char.isalnum():
                suffix = "  "
        return f"{prefix}{stylized}{suffix}"

    def _stylize_letters(self, content: str, transform: str = "preserve") -> str:
        if not content:
            return ""
        result: List[str] = []
        previous_alnum = False
        pending_delimiter: str = ""

        def apply_transform(char: str) -> str:
            if transform == "upper":
                return char.upper()
            if transform == "lower":
                return char.lower()
            return char

        for char in content:
            processed = apply_transform(char)
            if processed.isspace():
                if result and result[-1].isalnum():
                    pending_delimiter = "   "
                previous_alnum = False
                continue
            if processed.isalnum():
                if previous_alnum:
                    result.append(" ")
                elif pending_delimiter:
                    result.append(pending_delimiter)
                    pending_delimiter = ""
                elif result:
                    result.append("   ")
                result.append(processed)
                previous_alnum = True
            else:
                if result and result[-1] == " ":
                    result.pop()
                if pending_delimiter:
                    result.append(pending_delimiter.strip())
                    pending_delimiter = ""
                result.append(processed)
                previous_alnum = False
        stylized = "".join(result)
        return stylized.strip()

    def _stylize_delimited(
        self,
        content: str,
        delimiter: str,
        transform: str = "preserve",
        word_repeat: int = 2,
    ) -> str:
        def apply_transform(char: str) -> str:
            if transform == "upper":
                return char.upper()
            if transform == "lower":
                return char.lower()
            return char

        output: List[str] = []
        open_sequence = False
        pending_gap = False

        for char in content:
            if char.isspace():
                if open_sequence:
                    pending_gap = True
                continue

            processed = apply_transform(char)
            if not open_sequence:
                output.append(delimiter)
                open_sequence = True
            else:
                repeat = word_repeat if pending_gap else 1
                output.append(delimiter * repeat)
            output.append(processed)
            pending_gap = False

        if not open_sequence:
            return delimiter * 2

        output.append(delimiter)
        return "".join(output)

    def _handle_link(self, match: re.Match[str]) -> str:
        text, target = match.groups()
        url, _title = self._split_link_target(target)
        index = self._register_link(url)
        return f"[{text}]({index})"

    def _handle_image(self, match: re.Match[str]) -> str:
        alt_text, target = match.groups()
        url, _title = self._split_link_target(target)
        display_text = alt_text or "Image"
        index = self._register_link(url)
        return f"[Image: {display_text}]({index})"

    def _split_link_target(self, value: str) -> tuple[str, Optional[str]]:
        value = value.strip()
        if not value:
            return "", None
        if " " not in value:
            return value.strip(), None
        url, remainder = value.split(" ", 1)
        remainder = remainder.strip()
        if remainder.startswith('"') and remainder.endswith('"'):
            return url.strip(), remainder.strip('"')
        return url.strip(), remainder

    def _register_link(self, url: str) -> int:
        if url in self.link_indices:
            return self.link_indices[url]
        index = len(self.links) + 1
        self.links.append((index, url))
        self.link_indices[url] = index
        return index

    def _wrap_text(
        self,
        text: str,
        initial_indent: str = "",
        subsequent_indent: Optional[str] = None,
        style: Optional[BlockStyle] = None,
        hyphenate: bool = False,
    ) -> List[str]:
        style = style or BlockStyle()
        margin_left, _, available_width = self._margins(style)

        subsequent = initial_indent if subsequent_indent is None else subsequent_indent

        if hyphenate and self.hyphenator is not None:
            return self._wrap_text_hyphenated(
                text,
                initial_indent,
                subsequent,
                style,
                available_width,
            )

        wrapper = textwrap.TextWrapper(
            width=available_width,
            expand_tabs=False,
            replace_whitespace=False,
            drop_whitespace=False,
            break_on_hyphens=False,
            break_long_words=True,
            initial_indent=initial_indent,
            subsequent_indent=subsequent,
        )
        wrapped = wrapper.wrap(text)
        if not wrapped:
            wrapped = [initial_indent.rstrip()]
        result: List[str] = []
        for line in wrapped:
            line = line.rstrip()
            line_len = len(line)
            extra_space = max(0, available_width - line_len)
            if style.align == "center":
                extra_left = extra_space // 2
            elif style.align == "right":
                extra_left = extra_space
            else:
                extra_left = 0
            max_indent = max(0, self.width - line_len)
            indent = min(margin_left + extra_left, max_indent)
            result.append(" " * indent + line)
        return result

    def _wrap_text_hyphenated(
        self,
        text: str,
        initial_indent: str,
        subsequent_indent: str,
        style: BlockStyle,
        available_width: int,
    ) -> List[str]:
        tokens = re.split(r"(\s+)", text)
        lines: List[str] = []
        current_indent = initial_indent
        current_line = initial_indent
        current_len = len(current_line)
        width = available_width
        for index, token in enumerate(tokens):
            if token == "":
                continue
            if token.isspace():
                if current_len + len(token) > width and current_len > len(current_indent):
                    lines.append(current_line.rstrip())
                    current_indent = subsequent_indent
                    current_line = subsequent_indent
                    current_len = len(current_line)
                else:
                    current_line += token
                    current_len += len(token)
                continue
            segments = self._hyphenate_token(token) or [token]
            while segments:
                remaining = width - current_len
                if remaining <= 1:
                    lines.append(current_line.rstrip())
                    current_indent = subsequent_indent
                    current_line = subsequent_indent
                    current_len = len(current_line)
                    continue

                joined_length = sum(len(part) for part in segments)
                if current_len + joined_length <= width:
                    current_line += "".join(segments)
                    current_len += joined_length
                    segments = []
                    break

                split_index = None
                running = 0
                for idx in range(1, len(segments)):
                    running += len(segments[idx - 1])
                    needed = running + 1  # hyphen
                    if current_len + needed <= width:
                        split_index = idx
                    else:
                        break

                if split_index is None:
                    fragment = segments[0]
                    force_split = min(len(fragment), remaining - 1)
                    if force_split <= 0:
                        lines.append(current_line.rstrip())
                        current_indent = subsequent_indent
                        current_line = subsequent_indent
                        current_len = len(current_line)
                        continue
                    head = fragment[:force_split] + "-"
                    tail = fragment[force_split:]
                    current_line += head
                    lines.append(current_line.rstrip())
                    current_indent = subsequent_indent
                    current_line = subsequent_indent
                    current_len = len(current_line)
                    segments[0] = tail
                    if not tail:
                        segments.pop(0)
                    continue

                consumed_segments = segments[:split_index]
                current_line += "".join(consumed_segments) + "-"
                current_len += sum(len(part) for part in consumed_segments) + 1
                segments = segments[split_index:]
                lines.append(current_line.rstrip())
                current_indent = subsequent_indent
                current_line = subsequent_indent
                current_len = len(current_line)
        if current_line.strip():
            lines.append(current_line.rstrip())
        if not lines:
            lines.append(initial_indent.rstrip())

        result: List[str] = []
        margin_left, _, width = self._margins(style)
        for line in lines:
            stripped = line.rstrip()
            line_len = len(stripped)
            extra_space = max(0, width - line_len)
            if style.align == "center":
                extra_left = extra_space // 2
            elif style.align == "right":
                extra_left = extra_space
            else:
                extra_left = 0
            max_indent = max(0, self.width - line_len)
            indent = min(margin_left + extra_left, max_indent)
            result.append(" " * indent + stripped)
        return result

    def _hyphenate_token(self, token: str) -> Optional[List[str]]:
        if self.hyphenator is None:
            return None
        match = re.match(r"^([^A-Za-zÀ-ÖØ-öø-ÿ'’]*)([A-Za-zÀ-ÖØ-öø-ÿ'’]+)([^A-Za-zÀ-ÖØ-öø-ÿ'’]*)$", token)
        if not match:
            return None
        leading, word, trailing = match.groups()
        if len(word) <= 4:
            return None
        parts = self.hyphenator.hyphenate_word(word)
        if not parts:
            return None
        if isinstance(parts, str):
            segments = [segment for segment in parts.split("-") if segment]
        else:
            segments = [segment for segment in parts if segment]
        if len(segments) < 2:
            return None
        segments[0] = leading + segments[0]
        segments[-1] = segments[-1] + trailing
        return segments

    def _effective_width(self, style: BlockStyle) -> int:
        return self._margins(style)[2]

    def _ensure_header_spacing(self) -> None:
        if self.header_spacing <= 0:
            return
        existing = 0
        for line in reversed(self.output):
            if line.strip() == "":
                existing += 1
                if existing >= self.header_spacing:
                    return
            else:
                break
        self.output.extend([""] * (self.header_spacing - existing))
