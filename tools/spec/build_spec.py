"""Build "Agentic Analytics Spec v1.1.md" from spec_template.md and the project files.

    python3 tools/spec/build_spec.py            # rewrite the spec in the repo root
    python3 tools/spec/build_spec.py --check    # exit 1 if the committed spec is out of date

Placeholders in the template:
    <<FILE:path:lang>>   file from reference/agentic_analytics_demo/ (the spec's core project)
    <<PFILE:path:lang>>  file from agentic_analytics_demo/ (optional add-ons: dashboard, hosting)

Every file in the reference project must be embedded, so the spec stays complete. The output
must not contain private-use or zero-width characters (the v1.0 .docx problem).
Standard library only; run it with any Python 3.9+.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).resolve().with_name("spec_template.md")
REFERENCE = REPO / "reference" / "agentic_analytics_demo"
IMPLEMENTED = REPO / "agentic_analytics_demo"
OUTPUT = REPO / "Agentic Analytics Spec v1.1.md"

# Not embedded: generated or local-only files.
SKIP_PARTS = {".venv", ".pytest_cache", "__pycache__", "uv.lock", ".gitkeep"}


def _embedder(root: Path, used: list[str]):
    def embed(match: re.Match) -> str:
        rel, lang = match.group(1), match.group(2)
        body = (root / rel).read_text(encoding="utf-8").rstrip("\n")
        fence = "````" if "```" in body else "```"
        used.append(rel)
        return f"**`{rel}`**\n\n{fence}{lang}\n{body}\n{fence}"
    return embed


def build() -> tuple[str, list[str], list[str]]:
    text = TEMPLATE.read_text(encoding="utf-8")
    ref_used: list[str] = []
    add_used: list[str] = []
    text = re.sub(r"<<FILE:([^:>]+):([a-z]+)>>", _embedder(REFERENCE, ref_used), text)
    text = re.sub(r"<<PFILE:([^:>]+):([a-z]+)>>", _embedder(IMPLEMENTED, add_used), text)
    if "<<FILE" in text or "<<PFILE" in text:
        raise SystemExit("unresolved placeholder in template")
    bad = sorted({hex(ord(c)) for c in text if 0xE000 <= ord(c) <= 0xF8FF or c in "﻿​"})
    if bad:
        raise SystemExit(f"private-use or zero-width characters in output: {bad}")
    files = sorted(
        str(p.relative_to(REFERENCE))
        for p in REFERENCE.rglob("*")
        if p.is_file()
        and not (set(p.relative_to(REFERENCE).parts) & SKIP_PARTS)
        and p.name not in SKIP_PARTS
    )
    missing = [f for f in files if f not in ref_used]
    if missing:
        raise SystemExit(f"reference files not embedded in the spec: {missing}")
    return text, ref_used, add_used


def main() -> int:
    text, ref_used, add_used = build()
    summary = (f"{len(ref_used)} reference files + {len(add_used)} add-on files, "
               f"{text.count(chr(10))} lines")
    if "--check" in sys.argv[1:]:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.name} is out of date: run python3 tools/spec/build_spec.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT.name} is up to date ({summary})")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.name} ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
