from __future__ import annotations

import re
import string
import textwrap
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from md_types import (
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
        if event.kind is BlockKind.PARAGRAPH:
            self._render_paragraph(event.payload, event.style)
        elif event.kind is BlockKind.HEADING:
            self._render_heading(event.payload, event.style)
        elif event.kind is BlockKind.CODE_BLOCK:
            self._render_code_block(event.payload, event.style)
        elif event.kind is BlockKind.BLOCKQUOTE:
            self._render_blockquote(event.payload, event.style)
        elif event.kind is BlockKind.LIST_ITEM:
            self._render_list_item(event.payload, event.style)
        elif event.kind is BlockKind.HORIZONTAL_RULE:
            self._render_horizontal_rule(event.style)
        elif event.kind is BlockKind.BLANK_LINE:
            self._render_blank_line()
        else:  # pragma: no cover - defensive default
            self._last_stylable_block = None

    def _render_paragraph(self, payload: ParagraphPayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)

        def render(target_style: BlockStyle) -> List[str]:
            return self._wrap_text(processed, style=target_style, hyphenate=self.hyphenate)

        lines = render(style)
        self._emit_block(lines, stylable=True, render_fn=render, style=style)
        if self.paragraph_spacing > 0:
            self.output.extend([""] * self.paragraph_spacing)

    def _render_heading(self, payload: HeadingPayload, style: BlockStyle) -> None:
        self._ensure_header_spacing()

        def render(target_style: BlockStyle) -> List[str]:
            return self._render_heading_lines(payload.level, payload.text, target_style)

        lines = render(style)
        self._emit_block(lines, stylable=True, render_fn=render, style=style)

    def _render_code_block(self, payload: CodeBlockPayload, style: BlockStyle) -> None:
        lines = self._format_code_block(payload.lines, style)
        self._emit_block(lines, stylable=False)

    def _render_blockquote(self, payload: BlockQuotePayload, style: BlockStyle) -> None:
        processed = self._process_inline(payload.text)
        indent_unit = " | " if self.blockquote_bars else "   "
        indent = indent_unit * max(1, payload.depth)
        wrapped = self._wrap_text(
            processed,
            initial_indent=indent,
            subsequent_indent=indent,
            style=style,
            hyphenate=self.hyphenate,
        )
        self._emit_block(wrapped, stylable=False)

    def _render_list_item(self, payload: ListItemPayload, style: BlockStyle) -> None:
        base_indent = payload.indent.replace("\t", "    ")
        marker_indent = " " * self.list_marker_indent
        marker = payload.marker
        text_spacing = " " * self.list_text_spacing
        initial_indent = f"{base_indent}{marker_indent}{marker}{text_spacing}"
        subsequent_indent = f"{base_indent}{marker_indent}{' ' * len(marker)}{text_spacing}"
        processed = self._process_inline(payload.text)
        wrapped = self._wrap_text(
            processed,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
            style=style,
            hyphenate=self.hyphenate,
        )
        self._emit_block(wrapped, stylable=False)

    def _render_horizontal_rule(self, style: BlockStyle) -> None:
        line = self._render_horizontal_rule_line(style)
        self._emit_block([line], stylable=False)

    def _render_blank_line(self) -> None:
        if self.paragraph_spacing == 0:
            self.output.append("")

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

        margin_left = min(max(style.margin_left, 0), self.width - 1)
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

        margin_left = min(max(style.margin_left, 0), self.width - 1)
        available_width = max(1, self._effective_width(style))
        return " " * margin_left + "-" * available_width

    def _format_code_block(self, lines: List[str], style: BlockStyle) -> List[str]:
        if not lines:
            return []
        if self.wrap_code_blocks:
            if self.code_block_line_numbers:
                return self._format_wrapped_code_block_numbered(lines, style)
            return self._format_wrapped_code_block_plain(lines, style)
        if self.code_block_line_numbers:
            return self._format_code_block_numbered(lines, style)
        return self._format_code_block_plain(lines, style)

    def _format_code_block_numbered(self, lines: List[str], style: BlockStyle) -> List[str]:
        margin_indent = " " * max(0, style.margin_left)
        width = max(2, len(str(len(lines))))
        formatted: List[str] = []
        for idx, line in enumerate(lines, start=1):
            formatted.append(f"{margin_indent}{idx:0{width}d} | {line}")
        formatted.append(margin_indent if margin_indent else "")
        return formatted

    def _format_code_block_plain(self, lines: List[str], style: BlockStyle) -> List[str]:
        margin_indent = " " * max(0, style.margin_left)
        base_prefix = margin_indent + "   "
        formatted: List[str] = []
        for line in lines:
            if line:
                formatted.append(base_prefix + line)
            else:
                formatted.append(base_prefix)
        formatted.append(margin_indent if margin_indent else "")
        return formatted

    def _format_wrapped_code_block_numbered(self, lines: List[str], style: BlockStyle) -> List[str]:
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        margin_indent = " " * margin_left
        effective_width = self._effective_width(style)
        number_width = max(2, len(str(len(lines))))
        prefix_template = f"{margin_indent}{{num:0{number_width}d}} | "
        continuation_prefix = f"{margin_indent}{' ' * number_width} | "
        prefix_len_without_margin = len(prefix_template.format(num=0)) - len(margin_indent)
        content_width = max(1, effective_width - prefix_len_without_margin)
        formatted: List[str] = []
        for idx, line in enumerate(lines, start=1):
            if not line:
                formatted.append(prefix_template.format(num=idx))
                continue
            wrapped_segments = self._wrap_code_line_segments(line, content_width)
            if not wrapped_segments:
                formatted.append(prefix_template.format(num=idx))
                continue
            first_segment, *rest_segments = wrapped_segments
            formatted.append(prefix_template.format(num=idx) + first_segment)
            for segment in rest_segments:
                formatted.append(continuation_prefix + segment)
        formatted.append(margin_indent if margin_indent else "")
        return formatted

    def _format_wrapped_code_block_plain(self, lines: List[str], style: BlockStyle) -> List[str]:
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        margin_indent = " " * margin_left
        effective_width = self._effective_width(style)
        content_prefix = margin_indent + "   "
        content_width = max(1, effective_width - 3)
        formatted: List[str] = []
        for line in lines:
            if not line:
                formatted.append(content_prefix)
                continue
            wrapped_segments = self._wrap_code_line_segments(line, content_width)
            if not wrapped_segments:
                formatted.append(content_prefix)
                continue
            first_segment, *rest_segments = wrapped_segments
            formatted.append(content_prefix + first_segment)
            for segment in rest_segments:
                formatted.append(content_prefix + segment)
        formatted.append(margin_indent if margin_indent else "")
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

        text = re.sub(r"`[^`]*`", stash_code, text)

        text = self._apply_pattern(
            text,
            re.compile(r"~~(.*?)~~"),
            lambda m, src: self._stylize_delimited(m.group(1), "-", transform="preserve"),
        )
        text = self._apply_pattern(
            text,
            re.compile(r"\*\*(.*?)\*\*"),
            lambda m, src: self._replace_spaced_emphasis(src, m, transform="upper"),
        )
        text = self._apply_pattern(
            text,
            re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)"),
            lambda m, src: self._replace_spaced_emphasis(src, m, transform="preserve"),
        )

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
            re.compile(r"__(.*?)__"),
            lambda m: self._apply_emphasis_spacing(
                m.string,
                m.start(),
                m.end(),
                self._stylize_delimited(m.group(1), "_", transform="upper", word_repeat=3),
            ),
        )
        text = apply_with_placeholder(
            text,
            re.compile(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)"),
            lambda m: self._apply_emphasis_spacing(
                m.string,
                m.start(),
                m.end(),
                self._stylize_delimited(m.group(1), "_", transform="preserve", word_repeat=3),
            ),
        )

        text = re.sub(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)", self._handle_link, text)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", self._handle_image, text)

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
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        available_width = max(1, self._effective_width(style))

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
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        width = available_width
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
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        remaining = self.width - margin_left
        margin_right = min(max(style.margin_right, 0), max(0, remaining - 1))
        return max(1, self.width - margin_left - margin_right)

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
