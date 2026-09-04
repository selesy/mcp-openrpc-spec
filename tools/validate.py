#!/usr/bin/env python3
"""Validate the generated OpenRPC documents.

Checks, in order of how much they would hurt to get wrong:

1.  Each document validates against the canonical OpenRPC meta-schema
    (vendored, so this runs offline).
2.  Every internal ``$ref`` resolves.
3.  No JSON Schema 2020-12-only construct survived the downgrade, and no
    ``$ref`` carries sibling keywords that draft-07 would ignore.
4.  The method list matches the upstream ``ClientRequest``/``ClientNotification``
    unions exactly — no invented methods, none missed.
5.  Structural invariants: unique method and parameter names, by-name params,
    a required result on every non-notification method.
6.  Every `@deprecated` marker in the upstream TypeScript source is reflected
    in the document, since the upstream JSON Schema drops all of them.
7.  Every embedded example validates against the parameter and result schemas
    the document itself declares.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from jsonschema import Draft7Validator, RefResolver
except ImportError:  # pragma: no cover
    print("validate.py needs the `jsonschema` package (pip install jsonschema)", file=sys.stderr)
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deprecations import extract_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema"
SPEC = ROOT / "spec"

MCP_VERSION = "2026-07-28"
APPS_VERSION = "2026-01-26"

BANNED_KEYWORDS = ("$defs", "prefixItems", "unevaluatedProperties", "unevaluatedItems", "$dynamicRef", "$recursiveRef", "$anchor")

DOCUMENTS = [
    (SPEC / MCP_VERSION / "mcp-server.openrpc.json", "core"),
    (SPEC / MCP_VERSION / "mcp-server-extensions.openrpc.json", "extensions"),
    (SPEC / f"apps-{APPS_VERSION}" / "mcp-apps-ui.openrpc.json", "apps"),
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(message)
        return ok

    def fail(self, message: str) -> None:
        self.checks += 1
        self.failures.append(message)


def walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def resolve_pointer(document, pointer: str):
    node = document
    for part in pointer.lstrip("#/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(part)]
        elif part in node:
            node = node[part]
        else:
            return None
    return node


def meta_validate(document, report: Report, label: str) -> None:
    meta = json.loads((SCHEMA / "vendor/openrpc-meta.json").read_text())
    dialect = json.loads((SCHEMA / "vendor/json-schema-tools-meta.json").read_text())
    resolver = RefResolver.from_schema(
        meta,
        store={
            "https://meta.open-rpc.org/": meta,
            "https://meta.json-schema.tools/": dialect,
            "https://meta.json-schema.tools": dialect,
        },
    )
    validator = Draft7Validator(meta, resolver=resolver)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    report.check(not errors, f"{label}: {len(errors)} OpenRPC meta-schema violation(s)")
    for error in errors[:10]:
        report.failures.append(f"    at {'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message[:200]}")


def check_refs(document, report: Report, label: str) -> None:
    dangling = set()
    for path, node in walk(document):
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not ref.startswith("#/"):
                dangling.add(f"{label}: non-local $ref {ref} at {path}")
            elif resolve_pointer(document, ref) is None:
                dangling.add(f"{label}: dangling $ref {ref} at {path}")
            if len(node) > 1 and path.startswith("$.components.schemas"):
                dangling.add(f"{label}: $ref with siblings {sorted(set(node) - {'$ref'})} at {path}")
    for message in sorted(dangling):
        report.fail(message)
    report.check(True, f"{label}: refs resolve")


def check_dialect(document, report: Report, label: str) -> None:
    offenders = []
    for path, node in walk(document.get("components", {}).get("schemas", {})):
        if isinstance(node, dict):
            for keyword in BANNED_KEYWORDS:
                if keyword in node:
                    offenders.append(f"{label}: 2020-12 keyword {keyword} at components.schemas{path[1:]}")
            if "$schema" in node:
                offenders.append(f"{label}: stray $schema at components.schemas{path[1:]}")
    for message in offenders[:20]:
        report.fail(message)
    report.check(not offenders, f"{label}: schemas are draft-07 clean")


def upstream_method_names(source_path: Path, unions: tuple[str, ...]) -> set[str]:
    defs = json.loads(source_path.read_text())["$defs"]
    names: set[str] = set()

    def const_of(node) -> str | None:
        """Read the `method` const out of a request/notification definition."""
        if not isinstance(node, dict):
            return None
        for member in node.get("allOf", [node]):
            const = member.get("properties", {}).get("method", {}).get("const")
            if const:
                return const
        return None

    for union in unions:
        node = defs.get(union)
        if node is None:
            continue
        # A single-member union is inlined by the upstream generator rather
        # than emitted as a $ref, so both shapes have to be handled.
        for member in node.get("anyOf") or node.get("oneOf") or [node]:
            ref = member.get("$ref")
            target = defs.get(ref.rsplit("/", 1)[-1]) if ref else member
            found = const_of(target)
            if found:
                names.add(found)
    return names


def check_method_coverage(document, report: Report, label: str) -> None:
    declared = {method["name"] for method in document["methods"]}
    if label in ("core", "extensions"):
        expected = upstream_method_names(
            SCHEMA / f"mcp/{MCP_VERSION}/schema.json", ("ClientRequest", "ClientNotification")
        )
        if label == "extensions":
            expected |= {"tasks/get", "tasks/update", "tasks/cancel"}
    else:
        source = json.loads((SCHEMA / f"ext-apps/{APPS_VERSION}/schema.json").read_text())["$defs"]
        expected = set()
        for node in source.values():
            const = node.get("properties", {}).get("method", {}).get("const")
            if const:
                expected.add(const)
    missing = expected - declared
    extra = declared - expected
    report.check(not missing, f"{label}: methods missing from the document: {sorted(missing)}")
    report.check(not extra, f"{label}: methods not present upstream: {sorted(extra)}")


def check_structure(document, report: Report, label: str) -> None:
    names = [method["name"] for method in document["methods"]]
    report.check(len(names) == len(set(names)), f"{label}: duplicate method names")
    for method in document["methods"]:
        param_names = [param["name"] for param in method["params"]]
        report.check(
            len(param_names) == len(set(param_names)),
            f"{label}: {method['name']} has duplicate parameter names",
        )
        report.check(
            method.get("paramStructure") == "by-name",
            f"{label}: {method['name']} is not declared by-name",
        )
        if "result" in method:
            report.check(
                method["result"].get("required") is True,
                f"{label}: {method['name']} result is not marked required",
            )
        else:
            report.check(
                method.get("x-mcp-message-type") == "notification",
                f"{label}: {method['name']} omits result but is not marked a notification",
            )
        for param in method["params"]:
            report.check(
                "schema" in param,
                f"{label}: {method['name']} parameter {param['name']} has no schema",
            )


def check_deprecations(document, report: Report, label: str) -> None:
    """The upstream JSON Schema carries no deprecation information, so these
    flags are recovered from schema.ts. Verify none were lost in the wiring."""
    definitions, properties = extract_file(SCHEMA / f"mcp/{MCP_VERSION}/schema.ts")
    schemas = document["components"]["schemas"]
    for name in definitions:
        if name in schemas:
            report.check(
                schemas[name].get("deprecated") is True,
                f"{label}: {name} is deprecated upstream but not flagged",
            )
    for (owner, prop) in properties:
        schema = schemas.get(owner, {}).get("properties", {}).get(prop)
        if schema is not None:
            report.check(
                schema.get("deprecated") is True,
                f"{label}: {owner}.{prop} is deprecated upstream but not flagged",
            )
    flagged = sum(1 for schema in schemas.values() if schema.get("deprecated"))
    print(f"  {label}: {flagged} deprecated definition(s) flagged")


def check_examples(document, report: Report, label: str) -> None:
    resolver = RefResolver.from_schema(document)
    total = 0
    for method in document["methods"]:
        params = {param["name"]: param for param in method["params"]}
        required = {name for name, param in params.items() if param.get("required")}
        for example in method.get("examples", []):
            total += 1
            supplied = {entry["name"] for entry in example["params"]}
            report.check(
                supplied <= set(params),
                f"{label}: example {method['name']}/{example['name']} names undeclared params {sorted(supplied - set(params))}",
            )
            report.check(
                required <= supplied,
                f"{label}: example {method['name']}/{example['name']} omits required params {sorted(required - supplied)}",
            )
            for entry in example["params"]:
                param = params.get(entry["name"])
                if not param:
                    continue
                errors = list(Draft7Validator(param["schema"], resolver=resolver).iter_errors(entry["value"]))
                report.check(
                    not errors,
                    f"{label}: example {method['name']}/{example['name']} param {entry['name']} invalid: "
                    + "; ".join(e.message[:160] for e in errors[:3]),
                )
            if "result" in example and "result" in method:
                errors = list(
                    Draft7Validator(method["result"]["schema"], resolver=resolver).iter_errors(example["result"]["value"])
                )
                report.check(
                    not errors,
                    f"{label}: example {method['name']}/{example['name']} result invalid: "
                    + "; ".join(e.message[:160] for e in errors[:3]),
                )
    print(f"  {label}: validated {total} example pairing(s)")


def main() -> int:
    report = Report()
    for path, label in DOCUMENTS:
        if not path.exists():
            report.fail(f"{label}: {path.relative_to(ROOT)} not generated")
            continue
        print(f"checking {path.relative_to(ROOT)}")
        document = json.loads(path.read_text())
        meta_validate(document, report, label)
        check_refs(document, report, label)
        check_dialect(document, report, label)
        check_method_coverage(document, report, label)
        check_structure(document, report, label)
        if label in ("core", "extensions"):
            check_deprecations(document, report, label)
        check_examples(document, report, label)

    print()
    if report.failures:
        print(f"FAILED — {len(report.failures)} problem(s) across {report.checks} checks:", file=sys.stderr)
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"OK — {report.checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
