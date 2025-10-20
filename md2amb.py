#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import ama_renderer  # noqa: F401 - ensure AMA renderer plugin registration
from conversion_core import parse_frontmatter, run_conversion
from markdown_parser import MarkdownParser
from md_types import BlockStyle, FrontMatter
from plugins import get_parser_factory, get_renderer_factory, register_parser
from text_renderer import TextRenderer

# Ensure markdown parser registered for standalone usage


def _markdown_parser_factory(*, base_style: BlockStyle, **_: object) -> MarkdownParser:
    return MarkdownParser(base_style)


try:
    register_parser("markdown", _markdown_parser_factory)
except ValueError:
    pass


MARKDOWN_LINK_RE = re.compile(r"(\[[^\]]*\]\()([^)]+)(\))")
LOCAL_LINK_RE = re.compile(r"^[A-Za-z0-9_.~/\\-]+$")
EXT_MD = {".md", ".markdown", ".mkd", ".mkdn"}
AMA_MAX_BYTES = 65_535
AMB_MAGIC = b"AMB1"
LINK_CONTINUE_LABEL = "Continue"


@dataclass
class Article:
    source: Path
    ama_name: str


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert Markdown into an AMB archive.")
    parser.add_argument("input", type=Path, help="Root Markdown file to convert.")
    parser.add_argument("output", type=Path, help="Output AMB filename.")
    parser.add_argument("--title", type=str, help="Optional book title.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    input_path = args.input.resolve()
    if not input_path.exists():
        parser.error(f"Input file '{input_path}' does not exist.")

    amb_bytes = build_amb(
        root_markdown=input_path,
        title=args.title,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(amb_bytes)
    print(str(args.output))
    return 0


def build_amb(root_markdown: Path, title: str | None) -> bytes:
    articles = collect_articles(root_markdown)
    ama_contents = render_articles(articles)
    files = assemble_files(ama_contents, title)
    return pack_amb(files)


def collect_articles(root_markdown: Path) -> Dict[Path, Article]:
    queue: deque[Path] = deque([root_markdown])
    visited: Dict[Path, Article] = {}
    assigned_names: set[str] = set()

    while queue:
        current = queue.popleft()
        current = current.resolve()
        if current in visited:
            continue
        if not current.exists():
            raise FileNotFoundError(f"Referenced file '{current}' was not found.")
        if current == root_markdown:
            ama_name = "INDEX.AMA"
        else:
            ama_name = assign_ama_name(current.stem, assigned_names)
        assigned_names.add(ama_name)
        visited[current] = Article(source=current, ama_name=ama_name)

        for linked in find_local_markdown_links(current):
            queue.append(linked)

    return visited


def find_local_markdown_links(markdown_path: Path) -> List[Path]:
    text = markdown_path.read_text(encoding="utf-8")
    results: List[Path] = []

    for _, target, _ in MARKDOWN_LINK_RE.findall(text):
        cleaned = target.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if "://" in cleaned or cleaned.startswith(("mailto:", "ftp:", "gopher:", "tel:")):
            continue
        resolved = (markdown_path.parent / cleaned.split("#", 1)[0]).resolve()
        if resolved.suffix.lower() in EXT_MD:
            results.append(resolved)
    return results


def assign_ama_name(stem: str, existing: set[str]) -> str:
    base = "".join((c if c.isalnum() else "_") for c in stem.upper())
    if not base:
        base = "ARTICLE"
    if base[0].isdigit():
        base = f"_{base}"
    base = base[:8]

    name = f"{base}.AMA"
    counter = 1
    while name in existing:
        suffix = f"{counter:02d}"
        trimmed = base[: max(1, 8 - len(suffix))]
        name = f"{trimmed}{suffix}.AMA"
        counter += 1
    return name


def render_articles(articles: Dict[Path, Article]) -> Dict[str, List[str]]:
    parser_factory = get_parser_factory("markdown")
    renderer_factory = get_renderer_factory("ama")
    rendered: Dict[str, List[str]] = {}

    for path, article in articles.items():
        content = path.read_text(encoding="utf-8")
        rewritten = rewrite_links(content, path.parent, articles)
        frontmatter, body_lines = parse_frontmatter(rewritten.splitlines(keepends=True))
        ama_lines = run_conversion(
            body_lines,
            frontmatter=frontmatter,
            parser_factory=parser_factory,
            renderer_factory=renderer_factory,
            renderer_options={"width": 78},
            base_path=path.parent,
        )
        split_articles = split_article(article.ama_name, ama_lines)
        rendered.update(split_articles)
    return rendered


def rewrite_links(markdown: str, base_dir: Path, articles: Dict[Path, Article]) -> str:
    def replacer(match: re.Match[str]) -> str:
        prefix, target, suffix = match.groups()
        cleaned = target.strip()
        candidate = (base_dir / cleaned.split("#", 1)[0]).resolve()
        if candidate in articles:
            mapped = articles[candidate].ama_name
            return f"{prefix}{mapped}{suffix}"
        return match.group(0)

    return MARKDOWN_LINK_RE.sub(replacer, markdown)


def split_article(filename: str, lines: List[str]) -> Dict[str, List[str]]:
    encoded = "\n".join(lines).encode("utf-8")
    if len(encoded) <= AMA_MAX_BYTES:
        return {filename: lines}

    segments: List[List[str]] = []
    current: List[str] = []
    current_size = 0

    def flush_segment() -> None:
        nonlocal current, current_size
        if current:
            segments.append(current)
            current = []
            current_size = 0

    for line in lines:
        candidate_size = current_size + len((line + "\n").encode("utf-8"))
        if candidate_size > AMA_MAX_BYTES and current:
            flush_segment()
        current.append(line)
        current_size += len((line + "\n").encode("utf-8"))
    flush_segment()

    result: Dict[str, List[str]] = {}
    stem = Path(filename).stem
    generated_names = [filename]

    for idx in range(1, len(segments)):
        suffix = f"{idx:02d}"
        trimmed = stem[: max(1, 8 - len(suffix))]
        new_name = f"{trimmed}{suffix}.AMA"
        counter = 1
        while new_name in result or new_name in generated_names:
            suffix = f"{idx:02d}{counter}"
            trimmed = stem[: max(1, 8 - len(suffix))]
            new_name = f"{trimmed}{suffix}.AMA"
            counter += 1
        generated_names.append(new_name)

    for name, segment in zip(generated_names, segments, strict=False):
        result[name] = segment[:]

    for idx, name in enumerate(generated_names[:-1]):
        next_name = generated_names[idx + 1]
        result[name].append("")
        result[name].append(f"%l{next_name}:{LINK_CONTINUE_LABEL}%t")
    return result


def assemble_files(ama_contents: Dict[str, List[str]], title: str | None) -> List[Tuple[str, bytes]]:
    files: List[Tuple[str, bytes]] = []
    if title:
        files.append(("TITLE", title.encode("ascii", "ignore")[:64]))

    index_bytes = encode_ama("INDEX.AMA", ama_contents.pop("INDEX.AMA"))
    files.append(("INDEX.AMA", index_bytes))

    for name, lines in sorted(ama_contents.items()):
        files.append((name, encode_ama(name, lines)))

    return files


def encode_ama(name: str, lines: List[str]) -> bytes:
    content = "\n".join(lines).rstrip("\n") + "\n"
    data = content.encode("utf-8")
    if len(data) > AMA_MAX_BYTES:
        raise ValueError(f"Generated AMA article '{name}' exceeds {AMA_MAX_BYTES} bytes.")
    if any("\t" in line for line in lines):
        raise ValueError(f"Generated AMA article '{name}' contains tab characters.")
    return data


def pack_amb(files: List[Tuple[str, bytes]]) -> bytes:
    entries = []
    offset = 6 + 20 * len(files)
    payloads = []

    for filename, data in files:
        canonical = filename.upper()
        if len(canonical) > 12:
            raise ValueError(f"Filename '{canonical}' does not fit 8.3 constraints.")
        payloads.append(data)
        checksum = bsd_checksum(data)
        entries.append((canonical, offset, len(data), checksum))
        offset += len(data)

    output = bytearray()
    output.extend(AMB_MAGIC)
    output.extend(struct.pack("<H", len(entries)))

    for name, file_offset, length, checksum in entries:
        padded = name.encode("ascii", "ignore")
        padded = padded + b"\x00" * (12 - len(padded))
        output.extend(padded)
        output.extend(struct.pack("<I", file_offset))
        output.extend(struct.pack("<H", length))
        output.extend(struct.pack("<H", checksum))

    for data in payloads:
        output.extend(data)
    return bytes(output)


def bsd_checksum(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum = (checksum >> 1) | ((checksum & 1) << 15)
        checksum = (checksum + byte) & 0xFFFF
    return checksum


if __name__ == "__main__":
    raise SystemExit(main())
