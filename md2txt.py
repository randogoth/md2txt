#!/usr/bin/env python3
"""
Convert Markdown into 80-column DOS-compatible plain text.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from md_types import BlockStyle, FrontMatter
from markdown_parser import MarkdownParser
from text_renderer import TextRenderer


FRONTMATTER_PATTERN = re.compile(r"^---\s*$")


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


def convert_markdown(
    lines: Iterable[str],
    *,
    width: int,
    frontmatter: FrontMatter,
) -> List[str]:
    base_style = BlockStyle(
        align="left",
        margin_left=max(0, frontmatter.margin_left),
        margin_right=max(0, frontmatter.margin_right),
    )
    parser = MarkdownParser(base_style)
    renderer = TextRenderer(width=width, frontmatter=frontmatter)
    for event in parser.parse(lines):
        renderer.handle_event(event)
    return renderer.finalize()


def parse_frontmatter(lines: List[str]) -> Tuple[FrontMatter, List[str]]:
    if not lines or not FRONTMATTER_PATTERN.match(lines[0]):
        return FrontMatter(), lines
    frontmatter: dict[str, str] = {}
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
        figlet_fallback=_parse_bool(frontmatter.get("figlet_fallback"), False),
        header_spacing=max(0, _parse_int(frontmatter.get("header_spacing"), 2)),
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
    converted_lines = convert_markdown(content, width=args.width, frontmatter=frontmatter)
    write_output(args.output, converted_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

