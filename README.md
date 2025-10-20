```
                    $$\  $$$$$$\    $$\                 $$\     
                    $$ |$$  __$$\   $$ |                $$ |    
$$$$$$\$$$$\   $$$$$$$ |\__/  $$ |$$$$$$\   $$\   $$\ $$$$$$\   
$$  _$$  _$$\ $$  __$$ | $$$$$$  |\_$$  _|  \$$\ $$  |\_$$  _|  
$$ / $$ / $$ |$$ /  $$ |$$  ____/   $$ |     \$$$$  /   $$ |    
$$ | $$ | $$ |$$ |  $$ |$$ |        $$ |$$\  $$  $$<    $$ |$$\ 
$$ | $$ | $$ |\$$$$$$$ |$$$$$$$$\   \$$$$  |$$  /\$$\   \$$$$  |
\__| \__| \__| \_______|\________|   \____/ \__/  \__|   \____/ 
```

This repository contains the `md2txt` command line tool and supporting libraries for transforming Markdown into arcane formats that work well on retro hardware or constrained text viewers. A small plugin API lets you mix-and-match parsers and renderers so additional formats can plug into the same preprocessing pipeline.

## CLI

- `md2txt` – converts Markdown into plain text with extensive formatting support. It ships with the default `markdown` parser and `text` renderer plugins, registers optional `micron`, `ama`, and `gemini` renderers for Micron/Ancient Machine Book or Gemtext output, and exposes the core pipeline so you can add your own parser or renderer modules:
  - FIGlet-rendered headings.
  - Emphasis styles converted to spaced or delimited characters.
  - Optional alignment and margin controls via HTML `<p>` attributes or MultiMarkdown attribute blocks (e.g. `{:.center margin=20px}`) applied as leading spaces.
  - ASCII art injection with `#[label :align](art.txt)` syntax, supporting multiple art pieces per line and optional `{: .right}` style annotations.
  - Per-document toggles for code block wrapping, numbering, blockquote decoration, and list indentation spacing.
  - Link references can be emitted per block by setting `links_per_block: true` in frontmatter; otherwise they collect at the end of the document. Gemini output uses `=>` lines while the text renderer keeps numbered footnotes.

## Usage

```bash
md2txt input.md -o output.txt              # convert to DOS-friendly text
md2txt input.md                            # write result to stdout
md2txt input.md --width 72                 # override column width
md2txt input.md --renderer micron          # emit Micron-formatted output
md2txt input.md --renderer ama             # emit AMB/AMA markup
md2txt input.md --renderer gemini          # emit Gemtext for Gemini capsules
md2txt input.md --renderer-option width=68 # pass KEY=VALUE to a renderer
```

 - `--parser` and `--renderer` select a plugin by name (defaults are `markdown` and `text`).
 - Repeatable `--parser-option KEY=VALUE` and `--renderer-option KEY=VALUE` pairs are forwarded to the plugin factories as keyword arguments in addition to the defaults supplied by the CLI.
 - The CLI accepts `--help` for the full option list.

## Rendering Controls via Frontmatter

Fine-tune formatting by adding keys to frontmatter:

```yaml
---
h1_font: slant
h2_font: standard
h3_font: small
margin_left: 4
margin_right: 4
hyphenate: true
hyphen_lang: en_US
---
```

| Key | Description |
| --- | --- |
| `blockquote_bars: false` | Replace the default `|` prefix with three spaces. |
| `code_block_line_numbers: false` | Disable the `01 |` gutter in wrapped or unwrapped code blocks. |
| `code_block_wrap: <bool or int>` | Enables wrapping; when set to an integer it also controls the extra indentation applied to wrapped continuation lines. |
| `figlet_fallback: true` | force an H4-style fallback when a FIGlet banner would overflow; when omitted (the default), the banner is soft wrapping to honor width and margins. |
| `h#_font: <font>` | Accepts any known figlet font name or one of the keywords `caps` (force uppercase) and `title` (title case) to define headings.
| `hyphenate: true` | activates PyHyphen-based wrapping (optional `hyphen_lang` defaults to `en_US`) |
| `header_spacing` | controls how many blank lines precede headings (default `2`). |
| `links_per_block: true` | Emit link references directly below each block instead of collecting them at the end (supported by the text and Gemini renderers). |
| `list_marker_indent: <int>` | Extra spaces inserted before list markers. |
| `list_text_spacing: <int>` | Spaces between the marker and the wrapped list text. |
| `margin_left`/`margin_right` | add leading/trailing spaces to body text |
| `paragraph_spacing` | inserts blank lines between paragraphs |
| `wrap_code_blocks: true` | Wrap fenced/indented code blocks to fit the page width instead of using a gutter. |

All values are optional—defaults maintain the legacy behaviour.


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

## File Includes

Two lightweight include syntaxes are recognised during preprocessing:

- `![[relative/path.md]]`
- `{.include relative/path.md}`

The paths are resolved relative to the file that contains the directive. Any YAML frontmatter in the included file is stripped before inlining, and include cycles are detected and rejected.

## ASCII Art Blocks

Inline or block-level ASCII art can be embedded directly in Markdown:

```markdown
#[dragon :left](examples/dragon.txt)
#[dragon :right](examples/dragon.txt){:.center}
#[dragon_a :left](a.txt) #[dragon_b :center](b.txt) #[dragon_c :right](c.txt)
```

- `#[label](file)` loads the contents of the text file and inserts it as a preformatted block.
- Colon tags inside the label (`:left`, `:center`, `:right`) steer the alignment of each art piece. Additional colon tags are ignored.
- When multiple directives appear on the same line, the pieces are laid out side-by-side when space permits, otherwise they fall back to stacked blocks.

## Output Line Endings

Generated text uses CRLF line endings to maintain DOS compatibility.

## Plugin Architecture

The conversion pipeline lives in `src/md2txt/conversion/core.py` and is exposed via `run_conversion`. It is designed around lightweight factories:

- **Parser factories** receive `base_style: BlockStyle` plus any extra keyword arguments and must return an object with a `parse(lines: Iterable[str]) -> Iterator[BlockEvent | StyleUpdateEvent]` method. The bundled `MarkdownParser` implements this interface.
- **Renderer factories** receive `frontmatter: FrontMatter` and arbitrary keyword arguments and must return an object that provides `handle_event(event)` and `finalize() -> Any`. The `TextRenderer` returns a list of DOS-friendly output lines; other renderers may return any data appropriate for their target format.

Plugins register themselves through the helpers in `md2txt.plugins`:

```python
from md2txt.plugins import register_parser, register_renderer

def my_parser_factory(*, base_style, **options):
    return MyParser(base_style, **options)

register_parser("my-markdown", my_parser_factory)
```

```python
def my_renderer_factory(*, frontmatter, **options):
    return MyRenderer(frontmatter, target=options.get("target"))

register_renderer("ansi", my_renderer_factory)
```

Once registered (for example in a small module that imports `md2txt.cli`), the new plugins are available via `--parser my-markdown` or `--renderer ansi`. The CLI lists registered plugin names in `--help`, and the helper functions `available_parsers()` / `available_renderers()` return the sorted names if you need to build higher-level tooling.

The shared preprocessing helpers—YAML frontmatter parsing, recursive include expansion, and ASCII art sentinels—also live in `src/md2txt/conversion/core.py`, allowing alternate front-ends to reuse exactly the same behaviour without duplicating code.

---

* Directed and vibe coded by Tobias Raayoni Last
* Programming by gpt-5-codex