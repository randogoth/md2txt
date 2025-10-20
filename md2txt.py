#!/usr/bin/env python3
"""
Convert Markdown into 80-column DOS-compatible plain text.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from conversion_core import parse_frontmatter, read_lines, run_conversion
from md_types import BlockStyle, FrontMatter
from markdown_parser import MarkdownParser
from plugins import (
    available_parsers,
    available_renderers,
    get_parser_factory,
    get_renderer_factory,
    register_parser,
    register_renderer,
)
from text_renderer import TextRenderer

import micron_renderer  # noqa: F401  # register micron renderer plugin


def _split_option(token: str) -> Tuple[str, str]:
    if "=" not in token:
        raise argparse.ArgumentTypeError("Expected KEY=VALUE format.")
    key, value = token.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("Option key cannot be empty.")
    return key, value


def _markdown_parser_factory(*, base_style: BlockStyle, **_: Any) -> MarkdownParser:
    return MarkdownParser(base_style)


def _text_renderer_factory(*, frontmatter: FrontMatter, width: int = 80, **_: Any) -> TextRenderer:
    return TextRenderer(width=width, frontmatter=frontmatter)


try:
    register_parser("markdown", _markdown_parser_factory)
except ValueError:
    pass

try:
    register_renderer("text", _text_renderer_factory)
except ValueError:
    pass


def convert_markdown(
    lines: Iterable[str],
    *,
    width: int,
    frontmatter: FrontMatter,
    base_path: Optional[Path] = None,
    parser_name: str = "markdown",
    renderer_name: str = "text",
    parser_options: Optional[Dict[str, Any]] = None,
    renderer_options: Optional[Dict[str, Any]] = None,
) -> List[str]:
    parser_factory = get_parser_factory(parser_name)
    renderer_factory = get_renderer_factory(renderer_name)
    effective_renderer_options: Dict[str, Any] = {"width": width}
    if renderer_options:
        effective_renderer_options.update(renderer_options)
    rendered = run_conversion(
        lines,
        frontmatter=frontmatter,
        parser_factory=parser_factory,
        parser_options=parser_options,
        renderer_factory=renderer_factory,
        renderer_options=effective_renderer_options,
        base_path=base_path,
    )
    if isinstance(rendered, list):
        return rendered
    if isinstance(rendered, tuple):
        return list(rendered)
    if isinstance(rendered, str):
        return rendered.splitlines()
    raise TypeError("Renderer plugin returned unsupported output for md2txt CLI.")


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
    parser.add_argument(
        "--parser",
        default="markdown",
        choices=available_parsers() or ["markdown"],
        help="Name of the parser plugin to use.",
    )
    parser.add_argument(
        "--renderer",
        default="text",
        choices=available_renderers() or ["text"],
        help="Name of the renderer plugin to use.",
    )
    parser.add_argument(
        "--parser-option",
        action="append",
        default=[],
        type=_split_option,
        metavar="KEY=VALUE",
        help="Additional parser option in KEY=VALUE form (may repeat).",
    )
    parser.add_argument(
        "--renderer-option",
        action="append",
        default=[],
        type=_split_option,
        metavar="KEY=VALUE",
        help="Additional renderer option in KEY=VALUE form (may repeat).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    lines = read_lines(args.input_path)
    frontmatter, content = parse_frontmatter(lines)
    parser_options = dict(args.parser_option or [])
    renderer_options = dict(args.renderer_option or [])
    try:
        converted_lines = convert_markdown(
            content,
            width=args.width,
            frontmatter=frontmatter,
            base_path=args.input_path.parent,
            parser_name=args.parser,
            renderer_name=args.renderer,
            parser_options=parser_options or None,
            renderer_options=renderer_options or None,
        )
    except (KeyError, TypeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    write_output(args.output, converted_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
