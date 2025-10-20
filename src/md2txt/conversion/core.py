from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol, Set, Tuple, TypeVar

from ..models import BlockEvent, BlockStyle, FrontMatter, StyleUpdateEvent


FRONTMATTER_PATTERN = re.compile(r"^---\s*$")
INCLUDE_WIKILINK_PATTERN = re.compile(r"^\s*!\[\[(.+?)\]\]\s*$")
INCLUDE_DIRECTIVE_PATTERN = re.compile(r"^\s*\{\s*\.include\s+(.+?)\s*\}\s*$")
ASCII_BLOCK_PATTERN = re.compile(
    r"^\s*#\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\)\s*(?P<attr>\{\s*:[^}]+\s*\})?\s*$"
)
ASCII_INLINE_PATTERN = re.compile(r"#\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\)")
MMD_ATTR_TAIL_RE = re.compile(r"(.*?)\s*\{\s*:(.+?)\}\s*$")
ASCII_SENTINEL_PREFIX = "\u0000ASCII:"

Event = BlockEvent | StyleUpdateEvent
RendererOutput = TypeVar("RendererOutput")


class Parser(Protocol):
    def parse(self, lines: Iterable[str]) -> Iterator[Event]:
        ...


class Renderer(Protocol[RendererOutput]):
    def handle_event(self, event: Event) -> None:
        ...

    def finalize(self) -> RendererOutput:
        ...


class ParserFactory(Protocol):
    def __call__(self, *, base_style: BlockStyle, **kwargs: Any) -> Parser:
        ...


class RendererFactory(Protocol[RendererOutput]):
    def __call__(self, *, frontmatter: FrontMatter, **kwargs: Any) -> Renderer[RendererOutput]:
        ...


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
    default_wrap_indent = 2
    wrap_code_blocks = _parse_bool(frontmatter.get("wrap_code_blocks"), False)
    code_block_wrap_indent = default_wrap_indent if wrap_code_blocks else 0
    code_block_wrap_value = frontmatter.get("code_block_wrap")
    if code_block_wrap_value is not None:
        normalized_wrap = code_block_wrap_value.strip()
        if normalized_wrap:
            if re.fullmatch(r"-?\d+", normalized_wrap):
                wrap_code_blocks = True
                code_block_wrap_indent = max(0, _parse_int(normalized_wrap, default_wrap_indent))
            else:
                wrap_flag = _parse_bool(normalized_wrap, wrap_code_blocks)
                wrap_code_blocks = wrap_flag
                code_block_wrap_indent = default_wrap_indent if wrap_flag else 0
    code_block_line_numbers = _parse_bool(frontmatter.get("code_block_line_numbers"), True)
    blockquote_bars = _parse_bool(frontmatter.get("blockquote_bars"), True)
    list_marker_indent = max(0, _parse_int(frontmatter.get("list_marker_indent"), 0))
    list_text_spacing = max(0, _parse_int(frontmatter.get("list_text_spacing"), 1))
    links_per_block = _parse_bool(frontmatter.get("links_per_block"), False)
    fm = FrontMatter(
        h1_font=frontmatter.get("h1_font", "standard").strip() or "standard",
        h2_font=frontmatter.get("h2_font", "standard").strip() or "standard",
        h3_font=frontmatter.get("h3_font", "standard").strip() or "standard",
        margin_left=_parse_int(frontmatter.get("margin_left"), 0),
        margin_right=_parse_int(frontmatter.get("margin_right"), 0),
        paragraph_spacing=max(0, _parse_int(paragraph_spacing_value, 0)),
        hyphenate=_parse_bool(frontmatter.get("hyphenate"), False),
        hyphen_lang=(frontmatter.get("hyphen_lang") or "en_US").strip() or "en_US",
        figlet_fallback=_parse_bool(frontmatter.get("figlet_fallback"), False),
        header_spacing=max(0, _parse_int(frontmatter.get("header_spacing"), 2)),
        wrap_code_blocks=wrap_code_blocks,
        code_block_wrap_indent=code_block_wrap_indent,
        code_block_line_numbers=code_block_line_numbers,
        blockquote_bars=blockquote_bars,
        list_marker_indent=list_marker_indent,
        list_text_spacing=list_text_spacing,
        links_per_block=links_per_block,
    )
    return fm, remaining


def read_lines(path: Path) -> List[str]:
    with path.open("r", encoding="utf-8") as handle:
        return handle.readlines()


def expand_includes(
    lines: List[str],
    base_dir: Path,
    include_stack: Set[Path],
) -> List[str]:
    expanded: List[str] = []
    for line in lines:
        ascii_segments = _extract_ascii_segments(line, base_dir)
        if ascii_segments is not None:
            for sentinel_line, attr_line in ascii_segments:
                expanded.append(sentinel_line)
                if attr_line is not None:
                    expanded.append(attr_line)
            continue
        target = _extract_include_target(line)
        if target is None:
            expanded.append(line)
            continue
        target_path = (base_dir / target).resolve()
        if target_path in include_stack:
            raise RuntimeError(f"Circular include detected for '{target_path}'.")
        if not target_path.exists():
            raise FileNotFoundError(f"Included file '{target_path}' was not found.")
        include_stack.add(target_path)
        included_lines = read_lines(target_path)
        _, include_body = parse_frontmatter(included_lines)
        included_content = expand_includes(include_body, target_path.parent, include_stack)
        expanded.extend(included_content)
        include_stack.remove(target_path)
    return expanded


def _extract_include_target(line: str) -> Optional[str]:
    stripped = line.rstrip("\n")
    match = INCLUDE_WIKILINK_PATTERN.match(stripped)
    if match:
        return _normalize_include_target(match.group(1))
    match = INCLUDE_DIRECTIVE_PATTERN.match(stripped)
    if match:
        return _normalize_include_target(match.group(1))
    return None


def _normalize_include_target(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and ((trimmed[0] == trimmed[-1]) and trimmed[0] in {"'", '"'}):
        trimmed = trimmed[1:-1].strip()
    return trimmed


def _extract_ascii_segments(line: str, base_dir: Path) -> Optional[List[Tuple[str, Optional[str]]]]:
    stripped_line = line.rstrip("\n")
    block_match = ASCII_BLOCK_PATTERN.match(stripped_line)
    if block_match:
        label = block_match.group("label")
        target = block_match.group("target")
        attr_text = block_match.group("attr")
        sentinel = _make_ascii_sentinel(label, target, base_dir)
        attr_line = f"{attr_text}\n" if attr_text else None
        return [(sentinel, attr_line)]

    matches = list(ASCII_INLINE_PATTERN.finditer(stripped_line))
    if not matches:
        return None

    pieces: List[Dict[str, Optional[str]]] = []
    last_end = 0
    for match in matches:
        prefix = stripped_line[last_end : match.start()]
        if prefix.strip():
            return None
        label = match.group("label")
        target = match.group("target")
        block_type, block_name, align = _parse_ascii_label(label)
        normalized_target = _normalize_include_target(target)
        target_path = (base_dir / normalized_target).resolve()
        if not target_path.exists():
            raise FileNotFoundError(f"ASCII art file '{target_path}' was not found.")
        pieces.append(
            {
                "type": block_type,
                "name": block_name,
                "path": str(target_path),
                "align": align,
            }
        )
        last_end = match.end()

    suffix = stripped_line[last_end:]
    if suffix.strip():
        return None

    if not pieces:
        return None

    sentinel = f"{ASCII_SENTINEL_PREFIX}{json.dumps({'pieces': pieces})}\n"
    return [(sentinel, None)]


def _make_ascii_sentinel(label: str, target: str, base_dir: Path) -> str:
    block_type, block_name, align = _parse_ascii_label(label)
    normalized_target = _normalize_include_target(target)
    target_path = (base_dir / normalized_target).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"ASCII art file '{target_path}' was not found.")
    payload = {
        "pieces": [
            {
                "type": block_type,
                "name": block_name,
                "path": str(target_path),
                "align": align,
            }
        ]
    }
    return f"{ASCII_SENTINEL_PREFIX}{json.dumps(payload)}\n"


def _parse_ascii_label(label: str) -> Tuple[str, str, Optional[str]]:
    tokens = label.strip().split()
    non_colon: List[str] = []
    align: Optional[str] = None
    for token in tokens:
        if token.startswith(":"):
            tag = token[1:].strip().lower()
            if tag in {"left", "right", "center", "centre"}:
                align = "center" if tag in {"center", "centre"} else tag
        else:
            non_colon.append(token)

    block_type = non_colon[0] if non_colon else "custom"
    block_name = " ".join(non_colon[1:]) if len(non_colon) > 1 else ""
    return block_type, block_name, align


def run_pipeline(
    lines: Iterable[str],
    *,
    parser: Parser,
    renderer: Renderer[RendererOutput],
    base_path: Optional[Path] = None,
) -> RendererOutput:
    base_dir = (base_path or Path.cwd()).resolve()
    expanded_lines = expand_includes(list(lines), base_dir, set())
    for event in parser.parse(expanded_lines):
        renderer.handle_event(event)
    return renderer.finalize()


def run_conversion(
    lines: Iterable[str],
    *,
    frontmatter: FrontMatter,
    parser_factory: ParserFactory,
    renderer_factory: RendererFactory[RendererOutput],
    parser_options: Optional[Dict[str, Any]] = None,
    renderer_options: Optional[Dict[str, Any]] = None,
    base_path: Optional[Path] = None,
) -> RendererOutput:
    parser = parser_factory(
        base_style=BlockStyle(
            align="left",
            margin_left=max(0, frontmatter.margin_left),
            margin_right=max(0, frontmatter.margin_right),
        ),
        **(parser_options or {}),
    )
    renderer = renderer_factory(
        frontmatter=frontmatter,
        **(renderer_options or {}),
    )
    return run_pipeline(lines, parser=parser, renderer=renderer, base_path=base_path)
