#!/usr/bin/env python3
"""Shared helpers for turning MCP's JSON Schema definitions into OpenRPC pieces.

The upstream schemas are JSON Schema 2020-12; OpenRPC 1.x requires Schema
Objects to be JSON Schema draft-07. The downgrade this module performs is:

* ``$defs`` references are rewritten to ``#/components/schemas/...``.
* Per-definition ``$schema``/``$id`` keys are dropped (draft-07 has no
  vocabulary for them inside a subschema).
* ``$ref`` with sibling keywords becomes ``allOf: [{$ref}]`` plus the
  siblings.  Draft-07 ignores keywords beside ``$ref``, so the upstream
  ``{"$ref": ..., "description": ...}`` shape would silently lose its
  documentation; wrapping in ``allOf`` preserves both with identical
  semantics.

No other 2020-12-only keyword is used upstream (verified: no ``prefixItems``,
``unevaluatedProperties``, ``$dynamicRef``, or boolean ``exclusiveMinimum``).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable

DRAFT07 = "http://json-schema.org/draft-07/schema#"
LINK_RE = re.compile(r"\{@link\s+([^}]*)\}")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, document: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    path.write_text(body)
    return len(body)


class Source:
    """One vendored upstream schema file, indexed by definition name."""

    def __init__(self, path: Path, rename: dict[str, str] | None = None) -> None:
        self.path = path
        raw = read_json(path)
        self.defs: dict[str, Any] = raw.get("$defs") or raw.get("definitions") or {}
        self.rename = rename or {}

    # -- raw definition access -------------------------------------------------

    def node(self, name: str) -> Any:
        if name not in self.defs:
            raise KeyError(f"{self.path.name}: no definition named {name!r}")
        return self.defs[name]

    def merged(self, name: str) -> dict[str, Any]:
        """Flatten the ``allOf: [{$ref: envelope}, {...}]`` shape used by the
        extension schemas so that ``properties``/``required`` can be read
        uniformly with the core schema's flat request definitions."""
        node = self.node(name)
        if "allOf" not in node:
            return node
        properties: dict[str, Any] = {}
        required: list[str] = []
        description = node.get("description")
        for member in node["allOf"]:
            if "$ref" in member:  # the JSONRPCRequest/JSONRPCNotification envelope
                continue
            properties.update(member.get("properties", {}))
            required.extend(member.get("required", []))
            description = description or member.get("description")
        merged: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            merged["required"] = sorted(set(required))
        if description:
            merged["description"] = description
        return merged

    def method_name(self, name: str) -> str:
        node = self.merged(name)
        const = node.get("properties", {}).get("method", {}).get("const")
        if not const:
            raise KeyError(f"{self.path.name}: {name} has no method const")
        return const

    def params_object(self, name: str) -> dict[str, Any]:
        """Return the resolved params object schema for a request definition."""
        params = self.merged(name).get("properties", {}).get("params")
        if params is None:
            return {"type": "object", "properties": {}}
        if "$ref" in params:
            return self.node(ref_target(params["$ref"]))
        return params

    def result_schema(self, name: str) -> Any:
        """Return the schema of a method's result.

        The core schema wraps results in a ``*ResultResponse`` JSON-RPC
        envelope whose ``result`` property holds the union of possible
        shapes; the extension schemas instead define the result type
        directly, in which case a reference to the definition is returned.
        """
        node = self.merged(name)
        result = node.get("properties", {}).get("result")
        if result is not None:
            return result
        return {"$ref": f"#/$defs/{name}"}

    def description(self, name: str) -> str | None:
        return self.node(name).get("description")


def ref_target(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def clean_text(text: str, def_names: Iterable[str]) -> str:
    """Rewrite TSDoc ``{@link ...}`` markers into plain Markdown.

    The upstream JSON is generated from TypeScript, and the generator
    concatenates a link's target and its display text
    (``{@link CallToolRequesttools/call}``).  The definition names are known,
    so the target can be split off exactly rather than guessed.
    """
    names = sorted(def_names, key=len, reverse=True)

    def replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        target = next((n for n in names if body.startswith(n)), None)
        if target is None:
            return f"`{body}`"
        rest = body[len(target):].strip()
        if not rest:
            return f"`{target}`"
        if rest.startswith("."):  # {@link CallToolResult.content}
            return f"`{body}`"
        rest = rest.lstrip("|").strip()
        return f"`{rest}`" if "/" in rest and " " not in rest else rest

    return LINK_RE.sub(replace, text)


def downgrade(node: Any, rename: dict[str, str], def_names: Iterable[str]) -> Any:
    """Convert a 2020-12 subschema into an OpenRPC-compatible draft-07 one."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$schema", "$id"):
                continue
            if key == "$ref":
                out[key] = "#/components/schemas/" + rename.get(
                    ref_target(value), ref_target(value)
                )
            elif key == "description" and isinstance(value, str):
                out[key] = clean_text(value, def_names)
            else:
                out[key] = downgrade(value, rename, def_names)
        if "$ref" in out and len(out) > 1:
            ref = out.pop("$ref")
            out = {"allOf": [{"$ref": ref}], **out}
        return out
    if isinstance(node, list):
        return [downgrade(item, rename, def_names) for item in node]
    return node


def split_description(schema: Any, rename: dict[str, str], def_names: Iterable[str]):
    """Split a property schema into (schema, description).

    Hoisting the description onto the OpenRPC Content Descriptor keeps the
    common ``{"$ref": X, "description": D}`` shape from needing the
    ``allOf`` wrapper, and puts the prose where OpenRPC tooling shows it.
    """
    converted = downgrade(copy.deepcopy(schema), rename, def_names)
    description = None
    if isinstance(converted, dict) and "description" in converted:
        description = converted.pop("description")
        if set(converted) == {"allOf"} and len(converted["allOf"]) == 1:
            converted = converted["allOf"][0]
    return converted, description


def content_descriptors(
    params_object: dict[str, Any],
    rename: dict[str, str],
    def_names: Iterable[str],
    first: tuple[str, ...] = ("_meta",),
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten a params object schema into OpenRPC Content Descriptors.

    MCP always sends params by name, so each member of the params object
    becomes one named OpenRPC parameter and the method declares
    ``paramStructure: "by-name"``.
    """
    properties: dict[str, Any] = params_object.get("properties", {})
    required = set(params_object.get("required", []))
    overrides = overrides or {}

    def sort_key(name: str) -> tuple[int, int, str]:
        return (
            0 if name in first else 1,
            0 if name in required else 1,
            name,
        )

    descriptors = []
    for name in sorted(properties, key=sort_key):
        schema, description = split_description(properties[name], rename, def_names)
        descriptor: dict[str, Any] = {"name": name}
        if name in required:
            descriptor["required"] = True
        if description:
            descriptor["description"] = description
        override = overrides.get(name, {})
        if "description" in override:
            existing = descriptor.get("description")
            descriptor["description"] = (
                f"{existing}\n\n{override['description']}" if existing else override["description"]
            )
        descriptor["schema"] = schema
        for key, value in override.items():
            if key != "description":
                descriptor[key] = value
        descriptors.append(descriptor)
    return descriptors


def merge_defs(
    primary: dict[str, Any],
    secondary: dict[str, Any],
    prefix: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge a second source's definitions into the first.

    A definition that already exists identically is dropped; one that exists
    with different content is renamed ``<prefix><Name>`` and every reference
    to it inside the secondary source is rewritten accordingly.
    """
    canonical = {name: json.dumps(node, sort_keys=True) for name, node in primary.items()}
    rename: dict[str, str] = {}
    additions: dict[str, Any] = {}
    for name, node in secondary.items():
        stripped = {k: v for k, v in node.items() if k not in ("$schema", "$id")}
        if name in canonical:
            if json.dumps(stripped, sort_keys=True) == canonical[name] or json.dumps(
                node, sort_keys=True
            ) == canonical[name]:
                continue
            rename[name] = f"{prefix}{name}"
            additions[f"{prefix}{name}"] = node
        else:
            additions[name] = node
    return additions, rename
