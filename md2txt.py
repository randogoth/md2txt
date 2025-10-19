#!/usr/bin/env python3
"""
Convert Markdown into 80-column DOS-compatible plain text.
"""
from __future__ import annotations

import argparse
import re
import string
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

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


FRONTMATTER_PATTERN = re.compile(r"^---\s*$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
ORDERED_LIST_PATTERN = re.compile(r"^(\s*)(\d+\.)(\s+)(.*)$")
UNORDERED_LIST_PATTERN = re.compile(r"^(\s*)([*+-])(\s+)(.*)$")
BLOCKQUOTE_PATTERN = re.compile(r"^\s{0,3}>(.*)$")
HORIZONTAL_RULE_PATTERN = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
INLINE_PARA_RE = re.compile(r"^\s*<p\b([^>]*)>(.*?)</p>\s*$", re.IGNORECASE)
PARA_OPEN_RE = re.compile(r"^\s*<p\b([^>]*)>\s*$", re.IGNORECASE)
PARA_CLOSE_RE = re.compile(r"^\s*</p>\s*$", re.IGNORECASE)
MMD_ATTR_LINE_RE = re.compile(r"^\{\s*:(.+)\}\s*$")
MMD_ATTR_TAIL_RE = re.compile(r"(.*?)\s*\{\s*:(.+?)\}\s*$")


def _parse_int(value: Optional[str], default: int = 0) -> int:
    if value is None:
        return default
    match = re.search(r"-?\d+", value)
    if not match:
        return default
    try:
        return int(match.group())
    except ValueError:
        return default


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    return default


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
class BlockRecord:
    start: int
    length: int
    render: Callable[[BlockStyle], List[str]]
    style: BlockStyle


@dataclass
class FrontMatter:
    h1_font: str = "standard"
    h2_font: str = "standard"
    h3_font: str = "standard"
    margin_left: int = 0
    margin_right: int = 0
    paragraph_spacing: int = 0
    hyphenate: bool = False
    hyphen_lang: str = "en_US"


class MarkdownToTxtConverter:
    def __init__(self, width: int = 80, frontmatter: Optional[FrontMatter] = None) -> None:
        self.width = width
        self.frontmatter = frontmatter or FrontMatter()
        self.links: List[Tuple[int, str]] = []
        self.link_indices: Dict[str, int] = {}
        self.figlets: Dict[str, Figlet] = {}
        self._base_margin_left = max(0, self.frontmatter.margin_left)
        self._base_margin_right = max(0, self.frontmatter.margin_right)
        self.paragraph_spacing = max(0, self.frontmatter.paragraph_spacing)
        self.hyphenate = self.frontmatter.hyphenate
        self.hyphen_lang = self.frontmatter.hyphen_lang or "en_US"
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
        self._style_stack: List[BlockStyle] = [self._make_base_style()]
        self._paragraph_style_spec: Optional[StyleSpec] = None
        self._pending_block_style_spec: Optional[StyleSpec] = None
        self._last_stylable_block: Optional[BlockRecord] = None

    def convert(self, lines: Iterable[str]) -> List[str]:
        output: List[str] = []
        self.links = []
        self.link_indices = {}
        self._style_stack = [self._make_base_style()]
        self._paragraph_style_spec = None
        self._pending_block_style_spec = None
        self._last_stylable_block = None

        iterator = iter(lines)
        in_code_block = False
        code_lines: List[str] = []
        indented_code_lines: List[str] = []
        current_paragraph: List[str] = []

        for raw_line in iterator:
            line = raw_line.rstrip("\n")

            if in_code_block:
                if line.strip().startswith("```"):
                    self._emit_block(output, self._flush_code_block(code_lines))
                    code_lines = []
                    in_code_block = False
                else:
                    code_lines.append(line)
                continue

            if indented_code_lines:
                if line.startswith("    "):
                    indented_code_lines.append(line[4:])
                    continue
                if not line.strip():
                    self._emit_block(output, self._flush_code_block(indented_code_lines))
                    indented_code_lines = []
                else:
                    self._emit_block(output, self._flush_code_block(indented_code_lines))
                    indented_code_lines = []

            inline_para = INLINE_PARA_RE.match(line.strip())
            if inline_para:
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                spec = self._style_spec_from_html_attributes(inline_para.group(1) or "")
                self._push_style(spec)
                content = inline_para.group(2)
                if content:
                    current_paragraph.append(content)
                    self._flush_paragraph(current_paragraph, output)
                    current_paragraph = []
                self._pop_style()
                continue

            open_para = PARA_OPEN_RE.match(line)
            if open_para:
                spec = self._style_spec_from_html_attributes(open_para.group(1) or "")
                self._push_style(spec)
                continue

            close_para = PARA_CLOSE_RE.match(line)
            if close_para:
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                self._paragraph_style_spec = None
                self._pop_style()
                continue

            stripped = line.strip()
            attr_match = MMD_ATTR_LINE_RE.match(stripped)
            if attr_match:
                spec = self._parse_style_spec_from_tokens(attr_match.group(1))
                if spec:
                    if current_paragraph:
                        self._paragraph_style_spec = self._merge_specs(self._paragraph_style_spec, spec)
                    elif self._last_stylable_block is not None:
                        self._apply_style_to_last_block(output, spec)
                    else:
                        self._pending_block_style_spec = self._merge_specs(self._pending_block_style_spec, spec)
                continue

            if line.strip().startswith("```"):
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                in_code_block = True
                code_lines = []
                continue

            if line.startswith("    "):
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                indented_code_lines = [line[4:]]
                continue

            heading_match = HEADING_PATTERN.match(line)
            if heading_match:
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                heading_text, inline_spec = self._extract_trailing_attr(heading_text)
                combined_spec = self._merge_specs(self._pending_block_style_spec, inline_spec)
                style = self._combine_styles(self._current_style(), combined_spec)
                render = lambda s: self._render_heading_lines(level, heading_text, s)
                self._emit_block(output, render(style), stylable=True, render_fn=render, style=style)
                self._pending_block_style_spec = None
                continue

            if HORIZONTAL_RULE_PATTERN.match(line):
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                self._emit_block(output, [self._render_horizontal_rule(self._current_style())])
                continue

            if BLOCKQUOTE_PATTERN.match(line):
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                self._handle_blockquote(line, output)
                continue

            if UNORDERED_LIST_PATTERN.match(line) or ORDERED_LIST_PATTERN.match(line):
                self._flush_paragraph(current_paragraph, output)
                current_paragraph = []
                self._handle_list_line(line, output)
                continue

            if not line.strip():
                self._flush_paragraph(current_paragraph, output)
                if self.paragraph_spacing == 0:
                    output.append("")
                current_paragraph = []
                continue

            current_paragraph.append(line)

        self._flush_paragraph(current_paragraph, output)
        if in_code_block:
            self._emit_block(output, self._flush_code_block(code_lines))
        if indented_code_lines:
            self._emit_block(output, self._flush_code_block(indented_code_lines))

        if self.links:
            if output and output[-1] != "":
                output.append("")
            for index, url in self.links:
                entry = f"[{index}] {url}"
                output.extend(
                    self._wrap_text(
                        entry,
                        initial_indent="",
                        subsequent_indent="",
                        style=self._make_base_style(),
                    )
                )
            self._last_stylable_block = None

        return output

    def _flush_paragraph(self, paragraph_lines: List[str], output: List[str]) -> None:
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines)
        processed = self._process_inline(text)
        combined_spec = self._merge_specs(self._pending_block_style_spec, self._paragraph_style_spec)
        style = self._combine_styles(self._current_style(), combined_spec)

        def render(target_style: BlockStyle) -> List[str]:
            return self._wrap_text(processed, style=target_style, hyphenate=self.hyphenate)

        lines = render(style)
        self._emit_block(output, lines, stylable=True, render_fn=render, style=style)
        if self.paragraph_spacing > 0:
            output.extend([ "" for _ in range(self.paragraph_spacing) ])
        paragraph_lines.clear()
        self._paragraph_style_spec = None
        self._pending_block_style_spec = None

    def _flush_code_block(self, code_lines: List[str]) -> List[str]:
        if not code_lines:
            return []
        width = max(2, len(str(len(code_lines))))
        formatted: List[str] = []
        for idx, line in enumerate(code_lines, start=1):
            formatted.append(f" {idx:0{width}d} | {line}")
        formatted.append("")
        return formatted

    def _handle_blockquote(self, line: str, output: List[str]) -> None:
        # Normalize a single blockquote line before wrapping.
        depth = 0
        content = line
        while content.lstrip().startswith(">"):
            depth += 1
            content = content.lstrip()[1:]
        content = content.lstrip()
        processed = self._process_inline(content)
        indent_unit = " | "
        indent = indent_unit * max(1, depth)
        wrapped = self._wrap_text(
            processed,
            initial_indent=indent,
            subsequent_indent=indent,
            style=self._current_style(),
            hyphenate=self.hyphenate,
        )
        self._emit_block(output, wrapped)

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

    def _handle_list_line(self, line: str, output: List[str]) -> None:
        ordered = ORDERED_LIST_PATTERN.match(line)
        unordered = UNORDERED_LIST_PATTERN.match(line)
        if ordered:
            indent, marker, spacing, rest = ordered.groups()
        elif unordered:
            indent, marker, spacing, rest = unordered.groups()
        else:  # fall back to raw line
            self._emit_block(output, [line])
            return
        prefix = f"{indent}{marker}{spacing}"
        processed = self._process_inline(rest)
        wrapped = self._wrap_text(
            processed,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            style=self._current_style(),
            hyphenate=self.hyphenate,
        )
        self._emit_block(output, wrapped)

    def _emit_block(
        self,
        output: List[str],
        lines: List[str],
        *,
        stylable: bool = False,
        render_fn: Optional[Callable[[BlockStyle], List[str]]] = None,
        style: Optional[BlockStyle] = None,
    ) -> None:
        if not lines:
            return
        start = len(output)
        output.extend(lines)
        if stylable and render_fn is not None and style is not None:
            self._last_stylable_block = BlockRecord(start, len(lines), render_fn, style)
        else:
            self._last_stylable_block = None

    def _apply_style_to_last_block(self, output: List[str], spec: StyleSpec) -> None:
        if self._last_stylable_block is None:
            return
        new_style = self._combine_styles(self._last_stylable_block.style, spec)
        new_lines = self._last_stylable_block.render(new_style)
        start = self._last_stylable_block.start
        end = start + self._last_stylable_block.length
        output[start:end] = new_lines
        self._last_stylable_block.length = len(new_lines)
        self._last_stylable_block.style = new_style

    def _make_base_style(self) -> BlockStyle:
        return BlockStyle(
            align="left",
            margin_left=self._base_margin_left,
            margin_right=self._base_margin_right,
        )

    def _current_style(self) -> BlockStyle:
        return self._style_stack[-1]

    def _push_style(self, spec: Optional[StyleSpec]) -> None:
        base = self._current_style()
        self._style_stack.append(self._combine_styles(base, spec))

    def _pop_style(self) -> None:
        if len(self._style_stack) > 1:
            self._style_stack.pop()

    def _combine_styles(self, base: BlockStyle, spec: Optional[StyleSpec]) -> BlockStyle:
        if spec is None:
            return BlockStyle(align=base.align, margin_left=base.margin_left, margin_right=base.margin_right)
        return BlockStyle(
            align=spec.align or base.align,
            margin_left=spec.margin_left if spec.margin_left is not None else base.margin_left,
            margin_right=spec.margin_right if spec.margin_right is not None else base.margin_right,
        )

    def _merge_specs(self, first: Optional[StyleSpec], second: Optional[StyleSpec]) -> Optional[StyleSpec]:
        if first is None and second is None:
            return None
        if first is None:
            return second
        if second is None:
            return first
        return StyleSpec(
            align=second.align or first.align,
            margin_left=second.margin_left if second.margin_left is not None else first.margin_left,
            margin_right=second.margin_right if second.margin_right is not None else first.margin_right,
        )

    def _style_spec_from_html_attributes(self, attributes: str) -> Optional[StyleSpec]:
        if not attributes:
            return None
        attr_pattern = re.compile(r"([\w:-]+)\s*=\s*(\".*?\"|'.*?'|\S+)")
        attr_map: Dict[str, str] = {}
        for name, value in attr_pattern.findall(attributes):
            attr_map[name.lower()] = value.strip().strip("\"'")

        spec: Optional[StyleSpec] = None
        align_value = attr_map.get("align")
        if align_value:
            normalized = self._normalize_align(align_value)
            if normalized:
                spec = self._merge_specs(spec, StyleSpec(align=normalized))

        style_value = attr_map.get("style")
        if style_value:
            css_spec = self._style_spec_from_css(style_value)
            spec = self._merge_specs(spec, css_spec)

        return spec

    def _style_spec_from_css(self, css: str) -> Optional[StyleSpec]:
        spec = StyleSpec()
        changed = False
        for declaration in css.split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            name = name.strip().lower()
            value = value.strip()
            if not value:
                continue
            if name == "text-align":
                normalized = self._normalize_align(value)
                if normalized:
                    spec.align = normalized
                    changed = True
            elif name == "margin":
                left, right, auto_center = self._parse_css_margin_shorthand(value)
                if left is not None:
                    spec.margin_left = left
                    changed = True
                if right is not None:
                    spec.margin_right = right
                    changed = True
                if auto_center:
                    spec.align = "center"
                    changed = True
            elif name == "margin-left":
                parsed = self._parse_space_value(value)
                if parsed is not None:
                    spec.margin_left = parsed
                    changed = True
                elif value.lower() == "auto":
                    spec.align = spec.align or "center"
                    changed = True
            elif name == "margin-right":
                parsed = self._parse_space_value(value)
                if parsed is not None:
                    spec.margin_right = parsed
                    changed = True
                elif value.lower() == "auto":
                    spec.align = spec.align or "center"
                    changed = True
        return spec if changed else None

    def _parse_style_spec_from_tokens(self, token_str: str) -> Optional[StyleSpec]:
        tokens = re.split(r"\s+", token_str.strip())
        spec = StyleSpec()
        changed = False
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            if token.startswith("."):
                align = self._class_to_align(token[1:])
                if align:
                    spec.align = align
                    changed = True
                continue
            if "=" in token:
                key, value = token.split("=", 1)
                key = key.strip().lower().lstrip(".")
                value = value.strip().strip("\"'")
                if key in {"align", "text-align"}:
                    normalized = self._normalize_align(value)
                    if normalized:
                        spec.align = normalized
                        changed = True
                elif key in {"margin", "margin-left", "margin-right"}:
                    if key == "margin":
                        left, right, auto_center = self._parse_css_margin_shorthand(value)
                        if left is not None:
                            spec.margin_left = left
                            changed = True
                        if right is not None:
                            spec.margin_right = right
                            changed = True
                        if auto_center:
                            spec.align = "center"
                            changed = True
                    elif key == "margin-left":
                        parsed = self._parse_space_value(value)
                        if parsed is not None:
                            spec.margin_left = parsed
                            changed = True
                        elif value.lower() == "auto":
                            spec.align = spec.align or "center"
                            changed = True
                    elif key == "margin-right":
                        parsed = self._parse_space_value(value)
                        if parsed is not None:
                            spec.margin_right = parsed
                            changed = True
                        elif value.lower() == "auto":
                            spec.align = spec.align or "center"
                            changed = True
                continue
            align = self._normalize_align(token)
            if align:
                spec.align = align
                changed = True
        return spec if changed else None

    def _parse_css_margin_shorthand(self, value: str) -> Tuple[Optional[int], Optional[int], bool]:
        parts = [part for part in re.split(r"\s+", value.strip()) if part]
        if not parts:
            return None, None, False

        values: List[Optional[int]] = []
        autos: List[bool] = []
        for part in parts:
            if part.lower() == "auto":
                values.append(None)
                autos.append(True)
            else:
                parsed = self._parse_space_value(part)
                values.append(parsed)
                autos.append(False)

        left_auto = False
        right_auto = False
        if len(values) == 1:
            left = right = values[0]
            left_auto = right_auto = autos[0]
        elif len(values) == 2:
            left = right = values[1]
            left_auto = right_auto = autos[1]
        elif len(values) == 3:
            left = right = values[1]
            left_auto = right_auto = autos[1]
        else:
            right = values[1]
            left = values[3]
            right_auto = autos[1]
            left_auto = autos[3]
        auto_center = left_auto and right_auto
        return left, right, auto_center

    def _parse_space_value(self, value: str) -> Optional[int]:
        match = re.match(r"(-?\d+(?:\.\d+)?)", value.strip())
        if not match:
            return None
        number = float(match.group(1))
        return max(0, int(round(number)))

    def _normalize_align(self, value: str) -> Optional[str]:
        normalized = value.strip().lower()
        mapping = {
            "centre": "center",
            "center": "center",
            "left": "left",
            "right": "right",
        }
        return mapping.get(normalized)

    def _class_to_align(self, class_name: str) -> Optional[str]:
        name = class_name.strip().lower().lstrip(".")
        if name in {"center", "text-center", "align-center"}:
            return "center"
        if name in {"left", "text-left", "align-left"}:
            return "left"
        if name in {"right", "text-right", "align-right"}:
            return "right"
        return None

    def _extract_trailing_attr(self, text: str) -> Tuple[str, Optional[StyleSpec]]:
        match = MMD_ATTR_TAIL_RE.match(text)
        if not match:
            return text, None
        clean_text = match.group(1).rstrip()
        spec = self._parse_style_spec_from_tokens(match.group(2))
        return clean_text, spec

    def _render_heading_lines(self, level: int, text: str, style: BlockStyle) -> List[str]:
        font_name = getattr(self.frontmatter, f"h{level}_font", "standard")
        style_key = font_name.lower()
        if style_key in {"caps", "title"}:
            return self._render_h4_plus(text, style, transform=style_key)
        if level <= 3:
            figlet_lines = self._figlet_render(level, text)
            if figlet_lines:
                max_figlet_width = max((len(line.rstrip()) for line in figlet_lines), default=0)
                if max_figlet_width <= self._effective_width(style):
                    styled = self._apply_style_to_lines(figlet_lines, style)
                    return styled + [""]
        return self._render_h4_plus(text, style)

    def _render_horizontal_rule(self, style: BlockStyle) -> str:
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        available_width = max(1, self._effective_width(style))
        return " " * margin_left + "-" * available_width

    def _apply_style_to_lines(self, lines: List[str], style: BlockStyle) -> List[str]:
        if not lines:
            return []
        margin_left = min(max(style.margin_left, 0), self.width - 1)
        margin_right = max(style.margin_right, 0)
        available_width = max(1, self.width - margin_left - margin_right)
        block_width = max((len(line.rstrip()) for line in lines), default=0)
        block_width = min(block_width, available_width)
        extra_space = max(0, available_width - block_width)
        if style.align == "center":
            align_offset = extra_space // 2
        elif style.align == "right":
            align_offset = extra_space
        else:
            align_offset = 0
        max_indent = max(0, self.width - block_width)
        indent = min(margin_left + align_offset, max_indent)
        result: List[str] = []
        for line in lines:
            if not line:
                result.append("")
                continue
            trimmed = line.rstrip()
            result.append(" " * indent + trimmed)
        return result

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

    def _split_link_target(self, value: str) -> Tuple[str, Optional[str]]:
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

    def _figlet_render(self, level: int, text: str) -> Optional[List[str]]:
        if Figlet is None:
            return None
        font_name = getattr(self.frontmatter, f"h{level}_font", "standard")
        figlet = self.figlets.get(font_name)
        if figlet is None:
            try:
                figlet = Figlet(font=font_name)
            except (FontNotFound, TypeError):
                return None
            self.figlets[font_name] = figlet
        rendered = figlet.renderText(text).rstrip("\n")
        lines = rendered.splitlines()
        trimmed_lines = [line.rstrip("\n") for line in lines]
        if not trimmed_lines or max(len(line.rstrip()) for line in trimmed_lines) > self.width:
            return None
        return [line.rstrip() for line in trimmed_lines]

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
                    # fallback: break the first segment
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


def parse_frontmatter(lines: List[str]) -> Tuple[FrontMatter, List[str]]:
    if not lines or not FRONTMATTER_PATTERN.match(lines[0]):
        return FrontMatter(), lines
    frontmatter: Dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        if FRONTMATTER_PATTERN.match(lines[idx]):
            break
        if ":" in lines[idx]:
            key, value = lines[idx].split(":", 1)
            frontmatter[key.strip()] = value.strip()
        idx += 1
    if idx >= len(lines):
        return FrontMatter(), lines
    remaining = lines[idx + 1 :] if idx + 1 < len(lines) else []
    paragraph_spacing_value = frontmatter.get("paragraph_spacing")
    if paragraph_spacing_value is None:
        paragraph_spacing_value = frontmatter.get("lines_between_paragraphs")
    if paragraph_spacing_value is None:
        paragraph_spacing_value = frontmatter.get("paragraph_lines")
    fm = FrontMatter(
        h1_font=frontmatter.get("h1_font", "standard").strip() or "standard",
        h2_font=frontmatter.get("h2_font", "standard").strip() or "standard",
        h3_font=frontmatter.get("h3_font", "standard").strip() or "standard",
        margin_left=_parse_int(frontmatter.get("margin_left"), 0),
        margin_right=_parse_int(frontmatter.get("margin_right"), 0),
        paragraph_spacing=max(0, _parse_int(paragraph_spacing_value, 0)),
        hyphenate=_parse_bool(frontmatter.get("hyphenate"), False),
        hyphen_lang=(frontmatter.get("hyphen_lang") or "en_US").strip() or "en_US",
    )
    return fm, remaining


def read_lines(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8") as handle:
        return handle.readlines()


def write_output(path: Optional[Path], lines: List[str]) -> None:
    content = "\r\n".join(lines)
    if path is None:
        sys.stdout.write(content + "\r\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\r\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert Markdown files to 80-column DOS-compatible text.")
    parser.add_argument("input_path", type=Path, help="Path to the Markdown input file.")
    parser.add_argument("-o", "--output", type=Path, help="Optional path to write the resulting text file.")
    parser.add_argument("--width", type=int, default=80, help="Maximum column width (default: 80).")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    lines = read_lines(args.input_path)
    frontmatter, content = parse_frontmatter(lines)
    converter = MarkdownToTxtConverter(width=args.width, frontmatter=frontmatter)
    converted_lines = converter.convert(content)
    write_output(args.output, converted_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
