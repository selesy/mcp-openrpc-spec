#!/usr/bin/env python3
"""Fetch and pin the upstream schemas this repository derives its OpenRPC documents from.

Every source is pinned to an immutable commit SHA so that regenerating the
OpenRPC documents from the same checkout always produces byte-identical output.
Running this script rewrites schema/SOURCES.json with the sha256 of each file.

Usage:
    python3 tools/vendor.py            # fetch anything missing or changed
    python3 tools/vendor.py --check    # verify checksums only, fetch nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
SOURCES_FILE = SCHEMA_DIR / "SOURCES.json"

RAW = "https://raw.githubusercontent.com/modelcontextprotocol/{repo}/{sha}/{path}"

# --- Pinned upstream revisions ------------------------------------------------
# Bump a SHA here, re-run this script, then re-run tools/gen_openrpc.py.

MCP_REPO = "modelcontextprotocol"
MCP_SHA = "e76e9c572c6f2bfcb730357101acc90f2f802e02"
MCP_VERSION = "2026-07-28"

TASKS_REPO = "ext-tasks"
TASKS_SHA = "0d0a6bd4c258b35caa3c810a1dd506cf105b1501"
TASKS_VERSION = "2026-07-28"

APPS_REPO = "ext-apps"
APPS_SHA = "10195ad91851502134930e9b80ec2c04e277a720"
APPS_VERSION = "2026-01-26"

# Upstream example files paired into OpenRPC examplePairing objects, one entry
# per method: (request example def, response example def).
EXAMPLE_DEFS = [
    ("DiscoverRequest", "server-discover-request.json"),
    ("DiscoverResultResponse", "discover-result-response.json"),
    ("ListToolsRequest", "list-tools-request.json"),
    ("ListToolsResultResponse", "list-tools-result-response.json"),
    ("CallToolRequest", "call-tool-request.json"),
    ("CallToolResultResponse", "call-tool-result-response.json"),
    ("ListResourcesRequest", "list-resources-request.json"),
    ("ListResourcesResultResponse", "list-resources-result-response.json"),
    ("ReadResourceRequest", "read-resource-request.json"),
    ("ReadResourceResultResponse", "read-resource-result-response.json"),
    ("ReadResourceResultResponse", "read-resource-result-response-with-ttl.json"),
    ("ListResourceTemplatesRequest", "list-resource-templates-request.json"),
    ("ListResourceTemplatesResultResponse", "list-resource-templates-result-response.json"),
    ("ListPromptsRequest", "list-prompts-request.json"),
    ("ListPromptsResultResponse", "list-prompts-result-response.json"),
    ("GetPromptRequest", "get-prompt-request.json"),
    ("GetPromptResultResponse", "get-prompt-result-response.json"),
    ("CompleteRequest", "completion-request.json"),
    ("CompleteResultResponse", "completion-result-response.json"),
    ("SubscriptionsListenRequest", "listen-for-list-changes.json"),
    ("SubscriptionsListenResultResponse", "listen-closed-response.json"),
    ("CancelledNotification", "user-requested-cancellation.json"),
]


def sources() -> list[dict]:
    items = [
        {
            "local": f"mcp/{MCP_VERSION}/schema.json",
            "url": RAW.format(repo=MCP_REPO, sha=MCP_SHA, path=f"schema/{MCP_VERSION}/schema.json"),
        },
        {
            # The TypeScript source is vendored too: typescript-json-schema
            # drops JSDoc tags, so `@deprecated` markers exist only here.
            "local": f"mcp/{MCP_VERSION}/schema.ts",
            "url": RAW.format(repo=MCP_REPO, sha=MCP_SHA, path=f"schema/{MCP_VERSION}/schema.ts"),
        },
        {
            "local": f"ext-tasks/{TASKS_VERSION}/schema.json",
            "url": RAW.format(repo=TASKS_REPO, sha=TASKS_SHA, path=f"schema/{TASKS_VERSION}/schema.json"),
        },
        {
            "local": f"ext-apps/{APPS_VERSION}/schema.json",
            "url": RAW.format(repo=APPS_REPO, sha=APPS_SHA, path="src/generated/schema.json"),
        },
        # OpenRPC meta-schema and the JSON Schema dialect it references, vendored
        # so that tools/validate.py runs without network access.
        {
            "local": "vendor/openrpc-meta.json",
            "url": "https://meta.open-rpc.org/",
        },
        {
            "local": "vendor/json-schema-tools-meta.json",
            "url": "https://meta.json-schema.tools/",
        },
    ]
    for group, name in EXAMPLE_DEFS:
        items.append(
            {
                "local": f"mcp/{MCP_VERSION}/examples/{group}/{name}",
                "url": RAW.format(
                    repo=MCP_REPO, sha=MCP_SHA, path=f"schema/{MCP_VERSION}/examples/{group}/{name}"
                ),
            }
        )
    return items


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https hosts
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify checksums without fetching")
    args = parser.parse_args()

    recorded = {}
    if SOURCES_FILE.exists():
        recorded = {s["local"]: s for s in json.loads(SOURCES_FILE.read_text())["sources"]}

    manifest = []
    failures = []
    for item in sources():
        target = SCHEMA_DIR / item["local"]
        if args.check:
            if not target.exists():
                failures.append(f"missing: {item['local']}")
                continue
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            expected = recorded.get(item["local"], {}).get("sha256")
            if expected and digest != expected:
                failures.append(f"checksum mismatch: {item['local']}")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        body = fetch(item["url"])
        target.write_bytes(body)
        manifest.append(
            {
                "local": item["local"],
                "url": item["url"],
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        print(f"vendored {item['local']} ({len(body)} bytes)")

    if args.check:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        print("OK: all vendored sources match SOURCES.json" if not failures else "", file=sys.stderr)
        return 1 if failures else 0

    SOURCES_FILE.write_text(
        json.dumps(
            {
                "note": "Generated by tools/vendor.py. Do not edit by hand.",
                "pins": {
                    "modelcontextprotocol/modelcontextprotocol": {
                        "sha": MCP_SHA,
                        "protocolVersion": MCP_VERSION,
                    },
                    "modelcontextprotocol/ext-tasks": {"sha": TASKS_SHA, "version": TASKS_VERSION},
                    "modelcontextprotocol/ext-apps": {"sha": APPS_SHA, "version": APPS_VERSION},
                },
                "sources": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {SOURCES_FILE.relative_to(ROOT)} ({len(manifest)} sources)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
