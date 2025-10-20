from __future__ import annotations

import re
from typing import Iterable, Iterator, List, Optional, Union

from md_types import (
    BlockEvent,
    BlockKind,
    BlockQuotePayload,
    BlockStyle,
    CodeBlockPayload,
    HeadingPayload,
    ListItemPayload,
    ParagraphPayload,
    StyleSpec,
    StyleUpdateEvent,
)


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


class MarkdownParser:
    def __init__(self, base_style: BlockStyle) -> None:
        self._base_style = base_style
        self._style_stack: List[BlockStyle] = [self._make_base_style()]
        self._paragraph_style_spec: Optional[StyleSpec] = None
        self._pending_block_style_spec: Optional[StyleSpec] = None
        self._last_stylable_block: bool = False

    def parse(self, lines: Iterable[str]) -> Iterator[Union[BlockEvent, StyleUpdateEvent]]:
        self._reset_state()
        iterator = iter(lines)
        in_code_block = False
        code_lines: List[str] = []
        indented_code_lines: List[str] = []
        current_paragraph: List[str] = []

        for raw_line in iterator:
            line = raw_line.rstrip("\n")

            if in_code_block:
                if line.strip().startswith("```"):
                    event = self._flush_code_block(code_lines)
                    if event is not None:
                        yield event
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
                    event = self._flush_code_block(indented_code_lines)
                    if event is not None:
                        yield event
                    indented_code_lines = []
                else:
                    event = self._flush_code_block(indented_code_lines)
                    if event is not None:
                        yield event
                    indented_code_lines = []

            inline_para = INLINE_PARA_RE.match(line.strip())
            if inline_para:
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                spec = self._style_spec_from_html_attributes(inline_para.group(1) or "")
                self._push_style(spec)
                content = inline_para.group(2)
                if content:
                    current_paragraph.append(content)
                    event = self._flush_paragraph(current_paragraph)
                    if event is not None:
                        yield event
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
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
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
                    elif self._last_stylable_block:
                        yield StyleUpdateEvent(spec)
                    else:
                        self._pending_block_style_spec = self._merge_specs(self._pending_block_style_spec, spec)
                continue

            if line.strip().startswith("```"):
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                in_code_block = True
                code_lines = []
                continue

            if line.startswith("    "):
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                indented_code_lines = [line[4:]]
                continue

            heading_match = HEADING_PATTERN.match(line)
            if heading_match:
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                heading_text, inline_spec = self._extract_trailing_attr(heading_text)
                combined_spec = self._merge_specs(self._pending_block_style_spec, inline_spec)
                style = self._combine_styles(self._current_style(), combined_spec)
                self._pending_block_style_spec = None
                self._last_stylable_block = True
                yield BlockEvent(
                    kind=BlockKind.HEADING,
                    payload=HeadingPayload(level=level, text=heading_text),
                    style=style,
                    stylable=True,
                )
                continue

            if HORIZONTAL_RULE_PATTERN.match(line):
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                self._last_stylable_block = False
                yield BlockEvent(
                    kind=BlockKind.HORIZONTAL_RULE,
                    payload=None,
                    style=self._clone_style(),
                    stylable=False,
                )
                continue

            if BLOCKQUOTE_PATTERN.match(line):
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                quote_event = self._parse_blockquote(line)
                if quote_event is not None:
                    yield quote_event
                continue

            if UNORDERED_LIST_PATTERN.match(line) or ORDERED_LIST_PATTERN.match(line):
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                current_paragraph = []
                list_event = self._parse_list_line(line)
                if list_event is not None:
                    yield list_event
                continue

            if not line.strip():
                event = self._flush_paragraph(current_paragraph)
                if event is not None:
                    yield event
                yield BlockEvent(
                    kind=BlockKind.BLANK_LINE,
                    payload=None,
                    style=self._clone_style(),
                    stylable=False,
                )
                current_paragraph = []
                continue

            current_paragraph.append(line)

        event = self._flush_paragraph(current_paragraph)
        if event is not None:
            yield event
        if in_code_block:
            final_event = self._flush_code_block(code_lines)
            if final_event is not None:
                yield final_event
        if indented_code_lines:
            final_event = self._flush_code_block(indented_code_lines)
            if final_event is not None:
                yield final_event

    def _reset_state(self) -> None:
        self._style_stack = [self._make_base_style()]
        self._paragraph_style_spec = None
        self._pending_block_style_spec = None
        self._last_stylable_block = False

    def _flush_paragraph(self, paragraph_lines: List[str]) -> Optional[BlockEvent]:
        if not paragraph_lines:
            return None
        text = " ".join(line.strip() for line in paragraph_lines)
        combined_spec = self._merge_specs(self._pending_block_style_spec, self._paragraph_style_spec)
        style = self._combine_styles(self._current_style(), combined_spec)

        paragraph_lines.clear()
        self._paragraph_style_spec = None
        self._pending_block_style_spec = None
        self._last_stylable_block = True
        return BlockEvent(
            kind=BlockKind.PARAGRAPH,
            payload=ParagraphPayload(text=text),
            style=style,
            stylable=True,
        )

    def _flush_code_block(self, code_lines: List[str]) -> Optional[BlockEvent]:
        if not code_lines:
            return None
        lines = code_lines.copy()
        code_lines.clear()
        self._last_stylable_block = False
        return BlockEvent(
            kind=BlockKind.CODE_BLOCK,
            payload=CodeBlockPayload(lines=lines),
            style=self._clone_style(),
            stylable=False,
        )

    def _parse_blockquote(self, line: str) -> Optional[BlockEvent]:
        content = line
        depth = 0
        while content.lstrip().startswith(">"):
            depth += 1
            content = content.lstrip()[1:]
        text = content.lstrip()
        self._last_stylable_block = False
        return BlockEvent(
            kind=BlockKind.BLOCKQUOTE,
            payload=BlockQuotePayload(depth=max(1, depth), text=text),
            style=self._clone_style(),
            stylable=False,
        )

    def _parse_list_line(self, line: str) -> Optional[BlockEvent]:
        ordered = ORDERED_LIST_PATTERN.match(line)
        unordered = UNORDERED_LIST_PATTERN.match(line)
        if ordered:
            indent, marker, spacing, rest = ordered.groups()
            ordered_flag = True
        elif unordered:
            indent, marker, spacing, rest = unordered.groups()
            ordered_flag = False
        else:
            return None
        self._last_stylable_block = False
        return BlockEvent(
            kind=BlockKind.LIST_ITEM,
            payload=ListItemPayload(
                indent=indent,
                marker=marker,
                spacing=spacing,
                text=rest,
                ordered=ordered_flag,
            ),
            style=self._clone_style(),
            stylable=False,
        )

    def _make_base_style(self) -> BlockStyle:
        return BlockStyle(
            align=self._base_style.align,
            margin_left=self._base_style.margin_left,
            margin_right=self._base_style.margin_right,
        )

    def _current_style(self) -> BlockStyle:
        return self._style_stack[-1]

    def _clone_style(self) -> BlockStyle:
        style = self._current_style()
        return BlockStyle(
            align=style.align,
            margin_left=style.margin_left,
            margin_right=style.margin_right,
        )

    def _push_style(self, spec: Optional[StyleSpec]) -> None:
        base = self._current_style()
        self._style_stack.append(self._combine_styles(base, spec))

    def _pop_style(self) -> None:
        if len(self._style_stack) > 1:
            self._style_stack.pop()

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
        attr_map = {name.lower(): value.strip().strip("\"'") for name, value in attr_pattern.findall(attributes)}

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

    def _parse_css_margin_shorthand(self, value: str):
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

    def _extract_trailing_attr(self, text: str):
        match = MMD_ATTR_TAIL_RE.match(text)
        if not match:
            return text, None
        clean_text = match.group(1).rstrip()
        spec = self._parse_style_spec_from_tokens(match.group(2))
        return clean_text, spec
