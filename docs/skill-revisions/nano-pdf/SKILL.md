---
name: nano-pdf
description: "Edit text in existing PDFs via natural-language prompts."
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PDF, Documents, Editing, NLP, Productivity]
    homepage: https://pypi.org/project/nano-pdf/
    related_skills: [pdf, ocr-and-documents]
---

# nano-pdf

Edit PDFs using natural-language instructions. Point it at a page and describe what to change. For structural PDF work (merge, split, forms, watermarks, creation), see the `pdf` skill; for text extraction from scans, see `ocr-and-documents`.

## Prerequisites

```bash
# Install with uv (recommended — already available in Hermes)
uv pip install nano-pdf

# Or with pip
pip install nano-pdf
```

## Usage

```bash
nano-pdf edit <file.pdf> <page_number> "<instruction>"
```

## Examples

```bash
# Change a title on page 1
nano-pdf edit deck.pdf 1 "Change the title to 'Q3 Results' and fix the typo in the subtitle"

# Update a date on a specific page
nano-pdf edit report.pdf 3 "Update the date from January to February 2026"

# Fix content
nano-pdf edit contract.pdf 2 "Change the client name from 'Acme Corp' to 'Acme Industries'"
```

## Notes

- Before editing, verify the installed version’s page-number convention using its help or documentation. Work on a copy and target the exact intended page. If the wrong page changes, inspect the result and restore the copy before correcting the target; do not blindly retry with ±1.
- Verify the changed page’s extracted text and rendered appearance against the requested edit, and check that unrelated pages stayed unchanged. File size or successful process exit alone is not proof of a correct edit.
- The tool uses an LLM under the hood — requires an API key (check `nano-pdf --help` for config)
- Works well for text changes; complex layout modifications may need a different approach
