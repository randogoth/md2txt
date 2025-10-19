# md2amb utilities

This repository contains command line helpers for transforming Markdown into formats that work well on retro hardware or constrained text viewers.

## Tools

- `md2amb.py` – converts Markdown into Amber-screen formatted text (see script for details).
- `md2txt.py` – converts Markdown into 80-column, DOS-compatible plain text with extensive formatting support:
  - FIGlet-rendered headings (H1–H3) driven by optional YAML frontmatter (`h1_font`, `h2_font`, `h3_font`).
  - H4+ headings rendered in uppercase with dashed underlines.
  - Emphasis styles converted to spaced or delimited characters, e.g. `**bold**` → `B O L D`, `__strong__` → `_s_t_r_o_n_g_`, `~~strike~~` → `~s~t~r~i~k~e~`.
  - Blockquotes prefixed by `|    `, lists preserved, inline code untouched.
  - Code blocks numbered (`01 | line`), with both fenced and indented fences supported.
  - Links transformed into `[label](n)` references, with footnote-style URL list at the end.
  - Optional alignment and margin controls via HTML `<p>` attributes or MultiMarkdown attribute blocks (e.g. `{:.center margin=20px}`) applied as leading spaces.

## Requirements

- Python 3.9+.
- Optional: `PyHyphen` (preferred) or `pyphen` for dictionary-driven hyphenation.
- Optional: `pyfiglet` for FIGlet headings (`pip install pyfiglet`). When unavailable, headings fall back to the H4-style renderer automatically.

## Usage

```bash
python md2txt.py input.md -o output.txt          # convert to DOS-friendly text
python md2txt.py input.md                         # write result to stdout
python md2txt.py input.md --width 72             # override column width
```

Both scripts accept `--help` for the full option list.

## FIGlet Fonts via Frontmatter

You can set FIGlet fonts for H1–H3 per document using YAML frontmatter:

```markdown
---
h1_font: slant
h2_font: standard
h3_font: small
margin_left: 4
margin_right: 4
paragraph_spacing: 1
hyphenate: true
hyphen_lang: en_US
---

# Main Title
## Section Heading
### Subsection
```

Font names also accept the special keywords `caps` (force uppercase) and `title` (title case) for any heading. `margin_left` and `margin_right` add leading/trailing spaces to body text, `paragraph_spacing` (or `lines_between_paragraphs`) inserts blank lines between paragraphs, and enabling `hyphenate` activates PyHyphen-based wrapping (optional `hyphen_lang` defaults to `en_US`).

If the rendered FIGlet text would exceed the configured width, the converter automatically falls back to the H4-style uppercase heading with a dashed underline.

## Styling via Attribute Blocks

You can influence alignment and margins in the source Markdown using either HTML paragraphs or MultiMarkdown attribute blocks:

```markdown
<p align="center" style="margin: 10px 20px;">
Centered paragraph with custom margins.
</p>

Paragraph styled via MMD attributes.
{: .right margin=12px }

## Centered Heading {: .center margin=8px }
```

Margins are interpreted as spaces; values using `px` are rounded to the nearest integer, and `margin: 0 auto;` centers content.

## Output Line Endings

Generated text uses CRLF line endings to maintain DOS compatibility.
