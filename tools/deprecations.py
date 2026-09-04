#!/usr/bin/env python3
"""Recover `@deprecated` markers that the upstream JSON Schema drops.

MCP's `schema.json` is generated from `schema.ts` by typescript-json-schema,
which keeps descriptions but discards JSDoc tags.  In revision 2026-07-28 the
discarded tags matter a great deal: SEP-2577 deprecates sampling, roots, and
logging wholesale, and nothing in `schema.json` says so.  This module parses
the TypeScript source for those markers so the generated OpenRPC documents can
carry them.

``extract`` returns two maps:

* ``definitions``: definition name -> reason
* ``properties``: (definition name, property name) -> reason
"""

from __future__ import annotations

import re
from pathlib import Path

DECLARATION_RE = re.compile(r"^export\s+(?:interface|type)\s+(\w+)")
PROPERTY_RE = re.compile(r"^\s{2}(?:readonly\s+)?(\"[^\"]+\"|'[^']+'|[A-Za-z_$][\w$]*)\??\s*:")
TAG_RE = re.compile(r"^\s*@(\w+)")


def _reason(block: list[str]) -> str | None:
    """Pull the text of an ``@deprecated`` tag out of one JSDoc block."""
    collecting = False
    parts: list[str] = []
    for raw in block:
        line = re.sub(r"^\s*\*ic?\s?", "", raw.strip().lstrip("*")).strip()
        tag = TAG_RE.match(line)
        if tag:
            if tag.group(1) == "deprecated":
                collecting = True
                parts.append(line[len("@deprecated"):].strip())
                continue
            if collecting:
                break
            continue
        if collecting:
            if not line:
                break
            parts.append(line)
    if not collecting:
        return None
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip() or "Deprecated."


def extract(source: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    definitions: dict[str, str] = {}
    properties: dict[tuple[str, str], str] = {}

    current_def: str | None = None
    depth = 0
    block: list[str] | None = None
    pending: str | None = None

    for line in source.splitlines():
        stripped = line.strip()

        if block is not None:
            block.append(line)
            if "*/" in stripped:
                pending = _reason(block)
                block = None
            continue

        if stripped.startswith("/**"):
            block = [line]
            if "*/" in stripped[3:]:
                pending = _reason(block)
                block = None
            continue

        declaration = DECLARATION_RE.match(line)
        if declaration:
            current_def = declaration.group(1)
            depth = line.count("{") - line.count("}")
            if pending:
                definitions[current_def] = pending
            pending = None
            continue

        if current_def and depth > 0:
            member = PROPERTY_RE.match(line)
            if member and pending:
                name = member.group(1).strip("\"'")
                properties[(current_def, name)] = pending
                pending = None

        if current_def:
            depth += line.count("{") - line.count("}")
            if depth <= 0 and stripped:
                depth = 0

        if stripped and not stripped.startswith(("*", "//")):
            pending = None if not stripped.startswith("/**") else pending

    return definitions, properties


def extract_file(path: Path):
    return extract(path.read_text())


if __name__ == "__main__":
    import sys

    definitions, properties = extract_file(Path(sys.argv[1]))
    print(f"{len(definitions)} deprecated definition(s):")
    for name, reason in sorted(definitions.items()):
        print(f"  {name}: {reason[:90]}")
    print(f"{len(properties)} deprecated propert(y/ies):")
    for (owner, name), reason in sorted(properties.items()):
        print(f"  {owner}.{name}: {reason[:90]}")
