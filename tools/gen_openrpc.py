#!/usr/bin/env python3
"""Generate the OpenRPC documents in spec/ from the pinned schemas in schema/.

Three documents are produced:

  spec/2026-07-28/mcp-server.openrpc.json
      The core MCP server surface: the ten client-to-server methods plus the
      one client-to-server notification.

  spec/2026-07-28/mcp-server-tasks.openrpc.json
      The same surface plus every official extension that adds to it, which
      as of these pins is the Tasks extension alone.

  spec/apps-2026-01-26/mcp-apps-ui.openrpc.json
      The MCP Apps host <-> embedded view postMessage surface, which is a
      separate JSON-RPC service and not part of any MCP server.

Run tools/vendor.py first; run tools/validate.py afterwards.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deprecations import extract_file  # noqa: E402
from openrpc_lib import (  # noqa: E402
    Source,
    content_descriptors,
    downgrade,
    merge_defs,
    ref_target,
    split_description,
    write_json,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema"
SPEC = ROOT / "spec"

MCP_VERSION = "2026-07-28"
APPS_VERSION = "2026-01-26"
DOC_VERSION = "1.0.0"

# The published OpenRPC meta-schema at https://meta.open-rpc.org/ enumerates
# document versions up to 1.3.2, even though the prose specification at
# https://spec.open-rpc.org/ is numbered 1.4.x. 1.3.2 is declared so that these
# documents validate against the canonical meta-schema; nothing here uses a
# field introduced after it.
OPENRPC_VERSION = "1.3.2"

SPEC_BASE = f"https://modelcontextprotocol.io/specification/{MCP_VERSION}"
SCHEMA_ANCHOR = f"{SPEC_BASE}/schema"
DRAFT07 = "http://json-schema.org/draft-07/schema#"

LICENSE = {"name": "MIT", "url": "https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/LICENSE"}


# --------------------------------------------------------------------------- #
# Error catalogue
# --------------------------------------------------------------------------- #

ERRORS = {
    "ParseError": (-32700, "Parse error", "ParseError", "Invalid JSON was received."),
    "InvalidRequest": (
        -32600,
        "Invalid Request",
        "InvalidRequestError",
        "The payload is not a valid JSON-RPC request object.",
    ),
    "MethodNotFound": (
        -32601,
        "Method not found",
        "MethodNotFoundError",
        "The method does not exist, or is gated behind a server capability the "
        "server did not advertise. Over Streamable HTTP this is returned with "
        "HTTP 404.",
    ),
    "InvalidParams": (
        -32602,
        "Invalid params",
        "InvalidParamsError",
        "Unknown tool/prompt name, invalid arguments, or an invalid or expired "
        "pagination cursor. Replaces the -32002 resource-not-found code used by "
        "2025-11-25 and earlier.",
    ),
    "InternalError": (-32603, "Internal error", "InternalError", "An unexpected server-side condition."),
    "HeaderMismatch": (
        -32020,
        "Header mismatch",
        "HeaderMismatchError",
        "Streamable HTTP only: a mirrored HTTP header is missing, malformed, or "
        "disagrees with the request body. Returned with HTTP 400.",
    ),
    "MissingRequiredClientCapability": (
        -32021,
        "Missing required client capability",
        "MissingRequiredClientCapabilityError",
        "Serving the request needs a capability absent from this request's "
        "`_meta['io.modelcontextprotocol/clientCapabilities']`. The error `data` "
        "carries `requiredCapabilities`. Returned with HTTP 400.",
    ),
    "UnsupportedProtocolVersion": (
        -32022,
        "Unsupported protocol version",
        "UnsupportedProtocolVersionError",
        "The requested protocol version is unknown or unimplemented. The error "
        "`data` carries `supported` and `requested`. Returned with HTTP 400.",
    ),
}

# Errors any request-bearing method can produce.
COMMON_ERRORS = [
    "InvalidParams",
    "InternalError",
    "HeaderMismatch",
    "MissingRequiredClientCapability",
    "UnsupportedProtocolVersion",
]


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #

TAGS = {
    "discovery": "Protocol version, capability, and instruction discovery.",
    "tools": "Model-invocable functions the server exposes.",
    "resources": "Context and data the server exposes for the user or the model.",
    "prompts": "Templated messages and workflows the server exposes for users.",
    "completion": "Argument autocompletion for prompt and resource-template arguments.",
    "subscriptions": "Long-lived notification streams.",
    "utilities": "Cross-cutting protocol utilities.",
    "tasks": "Tasks extension: asynchronous, durable execution of long-running requests.",
    "view-to-host": "MCP Apps: sent by the embedded view to the host.",
    "host-to-view": "MCP Apps: sent by the host to the embedded view.",
    "sandbox": "MCP Apps: reserved messages exchanged with the sandbox proxy.",
}


# --------------------------------------------------------------------------- #
# Core method table
# --------------------------------------------------------------------------- #

META_NOTE = (
    "Required on every request. Carries "
    "`io.modelcontextprotocol/protocolVersion` and "
    "`io.modelcontextprotocol/clientCapabilities`, both of which are REQUIRED: "
    "this revision has no `initialize` handshake, so version and capabilities "
    "are re-declared per request and a server MUST NOT infer them from earlier "
    "requests."
)

MRTR_NOTE = (
    "Set only when retrying this request after an `input_required` result. "
    "See the Multi Round-Trip Requests pattern at "
    f"{SPEC_BASE}/basic/patterns/mrtr."
)

CORE_METHODS = [
    {
        "name": "server/discover",
        "request": "DiscoverRequest",
        "response": "DiscoverResultResponse",
        "tags": ["discovery"],
        "summary": "Advertise supported protocol versions, capabilities, and instructions.",
        "docs": f"{SPEC_BASE}/basic/versioning",
        "notes": (
            "Servers MUST implement this method; clients MAY skip it, because "
            "version negotiation can happen inline through per-request `_meta`. "
            "The result is a `CacheableResult`: honour `ttlMs` and `cacheScope` "
            "rather than re-discovering per request."
        ),
        "examples": [{"name": "discover", "request": "server-discover-request.json", "response": "discover-result-response.json"}],
    },
    {
        "name": "tools/list",
        "request": "ListToolsRequest",
        "response": "ListToolsResultResponse",
        "tags": ["tools"],
        "capability": "tools",
        "summary": "List the tools the server exposes.",
        "docs": f"{SPEC_BASE}/server/tools",
        "examples": [{"name": "listTools", "request": "list-tools-request.json", "response": "list-tools-result-response.json"}],
    },
    {
        "name": "tools/call",
        "request": "CallToolRequest",
        "response": "CallToolResultResponse",
        "tags": ["tools"],
        "capability": "tools",
        "summary": "Invoke a tool.",
        "docs": f"{SPEC_BASE}/server/tools",
        "header_name": "params.name",
        "notes": (
            "Tool results carry their own error channel: a tool that fails "
            "returns `isError: true` in a `CallToolResult`, not a JSON-RPC "
            "error. JSON-RPC errors are reserved for protocol-level failures "
            "such as an unknown tool name."
        ),
        "examples": [{"name": "callTool", "request": "call-tool-request.json", "response": "call-tool-result-response.json"}],
    },
    {
        "name": "resources/list",
        "request": "ListResourcesRequest",
        "response": "ListResourcesResultResponse",
        "tags": ["resources"],
        "capability": "resources",
        "summary": "List the concrete resources the server exposes.",
        "docs": f"{SPEC_BASE}/server/resources",
        "examples": [{"name": "listResources", "request": "list-resources-request.json", "response": "list-resources-result-response.json"}],
    },
    {
        "name": "resources/templates/list",
        "request": "ListResourceTemplatesRequest",
        "response": "ListResourceTemplatesResultResponse",
        "tags": ["resources"],
        "capability": "resources",
        "summary": "List the URI templates for parameterised resources.",
        "docs": f"{SPEC_BASE}/server/resources",
        "examples": [
            {
                "name": "listResourceTemplates",
                "request": "list-resource-templates-request.json",
                "response": "list-resource-templates-result-response.json",
            }
        ],
    },
    {
        "name": "resources/read",
        "request": "ReadResourceRequest",
        "response": "ReadResourceResultResponse",
        "tags": ["resources"],
        "capability": "resources",
        "summary": "Read the contents of a resource.",
        "docs": f"{SPEC_BASE}/server/resources",
        "header_name": "params.uri",
        "notes": (
            "An unknown URI is reported as `-32602` (Invalid params); the "
            "`-32002` resource-not-found code from 2025-11-25 and earlier is "
            "retired and never reused."
        ),
        "examples": [
            {"name": "readResource", "request": "read-resource-request.json", "response": "read-resource-result-response.json"},
            {
                "name": "readResourceWithCacheHint",
                "request": "read-resource-request.json",
                "response": "read-resource-result-response-with-ttl.json",
            },
        ],
    },
    {
        "name": "prompts/list",
        "request": "ListPromptsRequest",
        "response": "ListPromptsResultResponse",
        "tags": ["prompts"],
        "capability": "prompts",
        "summary": "List the prompts and prompt templates the server exposes.",
        "docs": f"{SPEC_BASE}/server/prompts",
        "examples": [{"name": "listPrompts", "request": "list-prompts-request.json", "response": "list-prompts-result-response.json"}],
    },
    {
        "name": "prompts/get",
        "request": "GetPromptRequest",
        "response": "GetPromptResultResponse",
        "tags": ["prompts"],
        "capability": "prompts",
        "summary": "Render a prompt, substituting the supplied arguments.",
        "docs": f"{SPEC_BASE}/server/prompts",
        "header_name": "params.name",
        "examples": [{"name": "getPrompt", "request": "get-prompt-request.json", "response": "get-prompt-result-response.json"}],
    },
    {
        "name": "completion/complete",
        "request": "CompleteRequest",
        "response": "CompleteResultResponse",
        "tags": ["completion"],
        "capability": "completions",
        "summary": "Offer autocompletion values for a prompt or resource-template argument.",
        "docs": f"{SPEC_BASE}/server/utilities/completion",
        "examples": [{"name": "complete", "request": "completion-request.json", "response": "completion-result-response.json"}],
    },
    {
        "name": "subscriptions/listen",
        "request": "SubscriptionsListenRequest",
        "response": "SubscriptionsListenResultResponse",
        "tags": ["subscriptions"],
        "summary": "Open a long-lived stream of opted-in change notifications.",
        "docs": f"{SPEC_BASE}/basic/patterns/subscriptions",
        "streaming": True,
        "notes": (
            "This request does not return promptly. Over Streamable HTTP the "
            "response is an SSE stream that stays open, delivering only the "
            "notification types named in `notifications` (each tagged with "
            "`_meta['io.modelcontextprotocol/subscriptionId']`) and finally a "
            "`SubscriptionsListenResult` when the stream closes. It replaces the "
            "`resources/subscribe`/`resources/unsubscribe` methods and the "
            "standalone HTTP GET stream of earlier revisions. Request-scoped "
            "notifications (`notifications/progress`, `notifications/message`) "
            "are never delivered here. OpenRPC has no vocabulary for a streaming "
            "result, so only the terminal result is described; see "
            "`x-mcp-streaming` on this method."
        ),
        "examples": [
            {
                "name": "listenForListChanges",
                "request": "listen-for-list-changes.json",
                "response": "listen-closed-response.json",
            }
        ],
    },
    {
        "name": "notifications/cancelled",
        "request": "CancelledNotification",
        "response": None,
        "tags": ["utilities"],
        "summary": "Tell the server a previously issued request is abandoned.",
        "docs": f"{SPEC_BASE}/basic/patterns/cancellation",
        "notification": True,
        "notes": (
            "The only client-to-server notification in the core protocol, and "
            "used on the stdio transport only: over Streamable HTTP, closing the "
            "request's SSE response stream *is* the cancellation signal and no "
            "such notification is sent. Servers send this notification too, but "
            "solely to terminate a `subscriptions/listen` stream on stdio."
        ),
        "examples": [{"name": "userRequestedCancellation", "request": "user-requested-cancellation.json", "response": None}],
    },
]

# Server-to-client notifications. Servers emit these; they are not part of the
# server's own callable surface, so they are recorded here and their payload
# schemas live in components.schemas.
SERVER_NOTIFICATIONS = [
    ("notifications/progress", "ProgressNotification", "Progress for an in-flight request that supplied a `progressToken`. Delivered on that request's response stream only."),
    ("notifications/message", "LoggingMessageNotification", "A log message, emitted only for requests whose `_meta` set the deprecated `io.modelcontextprotocol/logLevel`."),
    ("notifications/resources/updated", "ResourceUpdatedNotification", "A subscribed resource changed. Delivered on a `subscriptions/listen` stream."),
    ("notifications/resources/list_changed", "ResourceListChangedNotification", "The resource list changed. Delivered on a `subscriptions/listen` stream."),
    ("notifications/tools/list_changed", "ToolListChangedNotification", "The tool list changed. Delivered on a `subscriptions/listen` stream."),
    ("notifications/prompts/list_changed", "PromptListChangedNotification", "The prompt list changed. Delivered on a `subscriptions/listen` stream."),
    ("notifications/subscriptions/acknowledged", "SubscriptionsAcknowledgedNotification", "First message on a `subscriptions/listen` stream, echoing what the server actually subscribed the client to."),
    ("notifications/cancelled", "CancelledNotification", "stdio only, and only to terminate a `subscriptions/listen` stream."),
]

# Server-to-client interactions. In this revision these are NOT JSON-RPC
# requests: they are bare objects in the `inputRequests` map of an
# `input_required` result, answered by retrying the original request.
INPUT_REQUESTS = [
    ("sampling/createMessage", "CreateMessageRequest", "CreateMessageResult", "sampling"),
    ("roots/list", "ListRootsRequest", "ListRootsResult", "roots"),
    ("elicitation/create", "ElicitRequest", "ElicitResult", "elicitation"),
]

TRANSPORTS = {
    "stdio": {
        "docs": f"{SPEC_BASE}/basic/transports/stdio",
        "framing": "Newline-delimited JSON-RPC messages over the standard streams of a client-launched subprocess.",
        "cancellation": "The client sends a `notifications/cancelled` notification.",
    },
    "streamable-http": {
        "docs": f"{SPEC_BASE}/basic/transports/streamable-http",
        "endpoint": "A single server-provided path (the MCP endpoint) that accepts POST. GET and DELETE MUST be answered with 405.",
        "framing": "One HTTP POST per JSON-RPC message. The response is either `application/json` (one object) or `text/event-stream` (a request-scoped SSE stream carrying that request's notifications followed by the final response).",
        "cancellation": "Closing the request's SSE response stream. No `notifications/cancelled` message is sent.",
        "requiredRequestHeaders": {
            "MCP-Protocol-Version": "Must equal `params._meta['io.modelcontextprotocol/protocolVersion']`; a mismatch is -32020 with HTTP 400.",
            "Mcp-Method": "Must equal the JSON-RPC `method`.",
            "Mcp-Name": "Required for `tools/call`, `resources/read`, and `prompts/get`; mirrors `params.name` or `params.uri`.",
            "Accept": "Must list both `application/json` and `text/event-stream`.",
        },
        "optionalRequestHeaders": {
            "Mcp-Param-{Name}": "Mirrors a `tools/call` argument annotated with `x-mcp-header` in the tool's `inputSchema`. Servers MUST validate it against the body."
        },
        "responseHeaders": {"X-Accel-Buffering": "SHOULD be `no` on SSE responses so proxies do not buffer events."},
        "valueEncoding": "A header value that is not safely representable in ASCII is carried as `=?base64?<base64 of UTF-8>?=`.",
        "statusCodes": {
            "202": "Accepted notification (no body).",
            "400": "-32020 header mismatch, -32021 missing client capability, or -32022 unsupported protocol version.",
            "403": "Invalid `Origin` header.",
            "404": "-32601 method not found.",
            "405": "GET or DELETE against the MCP endpoint.",
        },
        "removedInThisRevision": [
            "The standalone HTTP GET SSE stream (replaced by `subscriptions/listen`).",
            "Protocol-level sessions and the `Mcp-Session-Id` header.",
            "Stream resumption via `Last-Event-ID`.",
            "Server-initiated JSON-RPC requests on SSE streams (replaced by the MRTR `input_required` flow).",
        ],
        "note": "The 2024-11-05 HTTP+SSE transport is deprecated upstream and is deliberately out of scope for these documents.",
    },
}

EXTENSION_PROSE = {
    "io.modelcontextprotocol/tasks": {
        "repository": "https://github.com/modelcontextprotocol/ext-tasks",
        "docs": "https://modelcontextprotocol.io/extensions/tasks/overview",
        "addsMethods": True,
        "summary": "Asynchronous execution of long-running requests, with polling, mid-flight input, and durable handles. The only official extension that adds methods to the MCP server surface, so it is the only one described as methods here.",
    },
    "io.modelcontextprotocol/ui": {
        "repository": "https://github.com/modelcontextprotocol/ext-apps",
        "docs": "https://modelcontextprotocol.io/extensions/apps/overview",
        "addsMethods": False,
        "summary": "MCP Apps. Adds no methods to the MCP server surface — only `_meta.ui` fields on tool and resource declarations. Its `ui/*` methods are a separate host <-> embedded view postMessage service, described in spec/apps-2026-01-26/mcp-apps-ui.openrpc.json.",
    },
    "io.modelcontextprotocol/oauth-client-credentials": {
        "repository": "https://github.com/modelcontextprotocol/ext-auth",
        "docs": "https://modelcontextprotocol.io/extensions/auth/oauth-client-credentials",
        "addsMethods": False,
        "summary": "OAuth 2.0 client-credentials flow for machine-to-machine authorization. Operates at the HTTP/OAuth layer; adds no JSON-RPC methods.",
    },
    "io.modelcontextprotocol/enterprise-managed-authorization": {
        "repository": "https://github.com/modelcontextprotocol/ext-auth",
        "docs": "https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization",
        "addsMethods": False,
        "summary": "Centralised access control for enterprise deployments. Operates at the HTTP/OAuth layer; adds no JSON-RPC methods.",
    },
    "server-card": {
        "repository": "https://github.com/modelcontextprotocol/ext-server-card",
        "addsMethods": False,
        "summary": "A static JSON metadata document for pre-connection discovery, fetched over HTTP (recommended location `GET <mcp-endpoint>/server-card`). Adds no JSON-RPC methods, and its advisory fields are not authoritative — `server/discover` is.",
    },
    "skills-over-mcp": {
        "repository": "https://github.com/modelcontextprotocol/ext-skills",
        "docs": "https://modelcontextprotocol.io/community/working-groups/skills-over-mcp",
        "addsMethods": False,
        "summary": "Skills discovery and distribution through existing MCP primitives. Still pre-SEP with no published schema; contributes `_meta` conventions and a skill URI scheme, not methods.",
    },
}


SEP_2577 = {
    "sep": "SEP-2577",
    "docs": f"{SPEC_BASE}/deprecated",
    "note": (
        "Revision 2026-07-28 deprecates sampling, roots, and logging. They "
        "remain in the specification for at least twelve months, so they are "
        "described here in full and flagged rather than omitted. Elicitation is "
        "the only client feature not deprecated."
    ),
}


def apply_deprecations(schemas: dict, definitions: dict, properties: dict) -> dict:
    """Flag deprecated definitions and properties in components.schemas.

    The upstream JSON Schema carries no deprecation information at all; these
    markers are recovered from the TypeScript source by tools/deprecations.py.
    """
    applied = {"definitions": [], "properties": []}
    for name, reason in definitions.items():
        schema = schemas.get(name)
        if schema is None:
            continue
        schema["deprecated"] = True
        schema["description"] = _with_deprecation(schema.get("description"), reason)
        applied["definitions"].append(name)
    for (owner, prop), reason in properties.items():
        schema = schemas.get(owner, {}).get("properties", {}).get(prop)
        if schema is None:
            continue
        schema["deprecated"] = True
        schema["description"] = _with_deprecation(schema.get("description"), reason)
        applied["properties"].append(f"{owner}.{prop}")
    return applied


def _with_deprecation(description: str | None, reason: str) -> str:
    marker = f"**Deprecated.** {reason}"
    return f"{marker}\n\n{description}" if description else marker


def error_components() -> dict:
    return {
        name: {"code": code, "message": message}
        for name, (code, message, _schema, _description) in ERRORS.items()
    }


def error_index() -> dict:
    """Root-level index of the error catalogue.

    OpenRPC's Error Object admits only `code`, `message`, and a `data` *value*
    — it has no slot for a schema describing `data`, and forbids `x-` fields.
    The structured payloads therefore live in components.schemas and are
    indexed here.
    """
    return {
        str(code): {
            "name": name,
            "message": message,
            "description": description,
            "schema": f"#/components/schemas/{schema}",
            "docs": f"{SCHEMA_ANCHOR}#{schema.lower()}",
        }
        for name, (code, message, schema, description) in ERRORS.items()
    }


def method_errors(spec: dict) -> list[dict]:
    names = list(COMMON_ERRORS)
    if spec.get("capability") or spec.get("gated"):
        names.insert(0, "MethodNotFound")
    return [{"$ref": f"#/components/errors/{name}"} for name in names]


def result_descriptor(source: Source, spec: dict, rename: dict, def_names, extra_refs=()) -> dict:
    """Build the OpenRPC result Content Descriptor for a method.

    The union of possible results is taken verbatim from upstream's
    `*ResultResponse` wrapper, so a method that can answer with an
    `input_required` result says so, and one that cannot does not.
    """
    schema = copy.deepcopy(source.result_schema(spec["response"]))
    schema = downgrade(schema, rename, def_names)
    members = schema.get("anyOf")
    if members is None:
        members = [schema]
    for ref in extra_refs:
        target = {"$ref": f"#/components/schemas/{ref}"}
        if target not in members:
            members.append(target)
    # Put the standard result first; `InputRequiredResult` and task handles are
    # alternates, and upstream's ordering is alphabetical rather than meaningful.
    alternates = ("InputRequiredResult", "CreateTaskResult", "TasksCreateTaskResult")
    members.sort(key=lambda member: ref_target(member.get("$ref", "")) in alternates)
    schema = members[0] if len(members) == 1 else {"anyOf": members}

    name = spec["response"]
    if name.endswith("Response"):
        name = name[: -len("Response")]
    descriptor_name = name[0].lower() + name[1:]

    description = (
        "Every result carries a `resultType` discriminator; a client MUST treat "
        "an absent `resultType` (from a pre-2026-07-28 server) as `\"complete\"`."
    )
    if len(members) > 1:
        description += (
            " More than one result shape is possible here — switch on "
            "`resultType` before reading any other field."
        )
    return {
        "name": descriptor_name,
        "required": True,
        "description": description,
        "schema": schema,
    }


def example_pairings(spec: dict, examples_dir: Path, param_names: list[str]) -> list[dict]:
    pairings = []
    for example in spec.get("examples", []):
        request_group = spec["request"]
        request_path = examples_dir / request_group / example["request"]
        if not request_path.exists():
            print(f"  ! missing example {request_path.relative_to(ROOT)}", file=sys.stderr)
            continue
        request = json.loads(request_path.read_text())
        params = request.get("params", {})
        unknown = [key for key in params if key not in param_names]
        if unknown:
            print(f"  ! example {example['name']} has undeclared params {unknown}", file=sys.stderr)
        pairing = {
            "name": example["name"],
            "params": [
                {"name": key, "value": params[key]}
                for key in sorted(params, key=lambda k: param_names.index(k) if k in param_names else 99)
            ],
        }
        if example.get("response"):
            response_path = examples_dir / spec["response"] / example["response"]
            if not response_path.exists():
                print(f"  ! missing example {response_path.relative_to(ROOT)}", file=sys.stderr)
            else:
                response = json.loads(response_path.read_text())
                pairing["result"] = {"name": "result", "value": response["result"]}
        pairings.append(pairing)
    return pairings


def build_method(
    source: Source,
    spec: dict,
    rename: dict,
    def_names,
    examples_dir: Path | None = None,
    extra_result_refs=(),
    param_overrides: dict | None = None,
) -> dict:
    declared = source.method_name(spec["request"])
    if declared != spec["name"]:
        raise SystemExit(f"method table says {spec['name']!r} but schema says {declared!r}")

    params_object = source.params_object(spec["request"])
    overrides = dict(param_overrides or {})
    if "_meta" in params_object.get("properties", {}) and not spec.get("notification"):
        overrides.setdefault("_meta", {"description": META_NOTE})
    for mrtr in ("inputResponses", "requestState"):
        if mrtr in params_object.get("properties", {}):
            overrides.setdefault(mrtr, {"description": MRTR_NOTE})

    params = content_descriptors(params_object, rename, def_names, overrides=overrides)

    description_parts = []
    upstream = source.description(spec["request"])
    if upstream:
        from openrpc_lib import clean_text

        description_parts.append(clean_text(upstream, def_names))
    if spec.get("notes"):
        description_parts.append(spec["notes"])

    method: dict = {"name": spec["name"]}
    if spec.get("summary"):
        method["summary"] = spec["summary"]
    if description_parts:
        method["description"] = "\n\n".join(description_parts)
    method["tags"] = [{"$ref": f"#/components/tags/{tag}"} for tag in spec["tags"]]
    method["paramStructure"] = "by-name"
    method["params"] = params

    if spec.get("response"):
        method["result"] = result_descriptor(source, spec, rename, def_names, extra_result_refs)

    if not spec.get("notification"):
        method["errors"] = method_errors(spec)

    if spec.get("docs"):
        method["externalDocs"] = {"description": "Specification", "url": spec["docs"]}

    if examples_dir is not None:
        pairings = example_pairings(spec, examples_dir, [p["name"] for p in params])
        if pairings:
            method["examples"] = pairings

    if spec.get("deprecated"):
        method["deprecated"] = True
    if spec.get("notification"):
        method["x-mcp-message-type"] = "notification"
        method["x-mcp-note"] = "No `result` is declared, which marks this method as notification-only in OpenRPC."
    if spec.get("capability"):
        method["x-mcp-server-capability"] = spec["capability"]
    if spec.get("header_name"):
        method["x-mcp-http-headers"] = {"Mcp-Method": "method", "Mcp-Name": spec["header_name"]}
    if spec.get("streaming"):
        method["x-mcp-streaming"] = {
            "kind": "server-push",
            "note": "The response is a long-lived stream, not a prompt reply. The declared result describes only the terminal message.",
            "notifications": [name for name, _def, _doc in SERVER_NOTIFICATIONS if "list_changed" in name or name.endswith("updated") or name.endswith("acknowledged")],
        }
    if spec.get("direction"):
        method["x-mcp-direction"] = spec["direction"]
    return method


def core_document(core: Source, examples_dir: Path, with_tasks: bool) -> dict:
    def_names = set(core.defs)
    schemas = {name: downgrade(node, {}, def_names) for name, node in sorted(core.defs.items())}
    deprecated_defs, deprecated_props = extract_file(SCHEMA / f"mcp/{MCP_VERSION}/schema.ts")
    applied = apply_deprecations(schemas, deprecated_defs, deprecated_props)

    rename: dict[str, str] = {}
    tasks: Source | None = None
    extra_tools_call: list[str] = []
    methods_extra: list[dict] = []

    if with_tasks:
        tasks = Source(SCHEMA / f"ext-tasks/{MCP_VERSION}/schema.json")
        additions, rename = merge_defs(core.defs, tasks.defs, "Tasks")
        if rename:
            print(f"  tasks definitions renamed to avoid collisions: {sorted(rename)}")
        task_def_names = set(tasks.defs)
        for name, node in sorted(additions.items()):
            schemas[name] = downgrade(node, rename, task_def_names)
        extra_tools_call = [rename.get("CreateTaskResult", "CreateTaskResult")]
        methods_extra = task_methods(tasks, rename, task_def_names)

    methods = []
    for spec in CORE_METHODS:
        spec = dict(spec, deprecated=spec["request"] in deprecated_defs)
        extra = extra_tools_call if (with_tasks and spec["name"] == "tools/call") else ()
        overrides = None
        if with_tasks and spec["name"] == "subscriptions/listen":
            overrides = {
                "notifications": {
                    "description": (
                        "With the Tasks extension negotiated, this filter also "
                        "accepts `taskIds` (see "
                        "`#/components/schemas/TaskSubscriptionNotifications`) to "
                        "receive `notifications/tasks` for those tasks. A server "
                        "MUST return -32021 if a client that did not declare the "
                        "extension asks for task notifications."
                    )
                }
            }
        methods.append(
            build_method(
                core,
                spec,
                {},
                def_names,
                examples_dir,
                extra_result_refs=extra,
                param_overrides=overrides,
            )
        )
    methods.extend(methods_extra)

    title = "Model Context Protocol — Server Surface"
    if with_tasks:
        title += " with Official Extensions"

    document = {
        "$schema": "https://meta.open-rpc.org/",
        "openrpc": OPENRPC_VERSION,
        "info": {
            "title": title,
            "version": DOC_VERSION,
            "description": core_description(with_tasks),
            "license": LICENSE,
            "x-mcp-protocol-version": MCP_VERSION,
        },
        "externalDocs": {"description": "Model Context Protocol specification", "url": SPEC_BASE},
        "servers": [
            {
                "name": "streamable-http",
                "url": "https://{host}{mcpEndpoint}",
                "summary": "The MCP endpoint of a Streamable HTTP server.",
                "description": (
                    "A single path that accepts POST. The URL is deployment-specific; "
                    "the variables below are placeholders, not defaults MCP defines. "
                    "OpenRPC has no vocabulary for the stdio transport, which has no "
                    "URL — see `x-mcp-transports`."
                ),
                "variables": {
                    "host": {"default": "localhost:3000", "description": "Authority of the MCP server."},
                    "mcpEndpoint": {"default": "/mcp", "description": "Path of the MCP endpoint."},
                },
                "x-mcp-transport": "streamable-http",
            }
        ],
        "methods": methods,
        "components": {
            "tags": {name: {"name": name, "description": description} for name, description in TAGS.items()},
            "errors": error_components(),
            "schemas": schemas,
        },
        "x-mcp-json-schema": {
            "sourceDialect": "https://json-schema.org/draft/2020-12/schema",
            "documentDialect": DRAFT07,
            "note": (
                "OpenRPC 1.x requires Schema Objects to be draft-07. The upstream "
                "definitions are 2020-12 and were downgraded mechanically: `$defs` "
                "references rewritten to `#/components/schemas/...`, subschema "
                "`$schema`/`$id` dropped, and `$ref` with sibling keywords wrapped "
                "as `allOf: [{$ref}]` so descriptions survive. No other 2020-12-only "
                "keyword is used upstream."
            ),
        },
        "x-mcp-sources": read_sources(),
        "x-mcp-transports": TRANSPORTS,
        "x-mcp-error-codes": error_index(),
        "x-mcp-server-notifications": {
            "note": (
                "Servers emit these; clients never call them, so they are not "
                "methods of this service. Payload schemas are in "
                "components.schemas. On Streamable HTTP, request-scoped "
                "notifications ride the originating request's SSE stream and "
                "change notifications ride a `subscriptions/listen` stream."
            ),
            "notifications": [
                {
                    "method": name,
                    "schema": f"#/components/schemas/{definition}",
                    "description": description,
                    **({"deprecated": True, "deprecationReason": deprecated_defs[definition]} if definition in deprecated_defs else {}),
                }
                for name, definition, description in SERVER_NOTIFICATIONS
            ],
        },
        "x-mcp-input-requests": {
            "note": (
                "Server-to-client interactions. In this revision they are NOT "
                "JSON-RPC requests and have no `jsonrpc` or `id`: the server "
                "returns an `InputRequiredResult` whose `inputRequests` map holds "
                "these bare objects, and the client answers by re-sending the "
                "original request with a matching `inputResponses` map plus the "
                "opaque `requestState`. Nothing may be inferred about ordering "
                "across retries."
            ),
            "pattern": f"{SPEC_BASE}/basic/patterns/mrtr",
            "requests": [
                {
                    "name": name,
                    "request": f"#/components/schemas/{request}",
                    "response": f"#/components/schemas/{response}",
                    "requiredClientCapability": capability,
                    **({"deprecated": True, "deprecationReason": deprecated_defs[request]} if request in deprecated_defs else {}),
                }
                for name, request, response, capability in INPUT_REQUESTS
            ],
        },
        "x-mcp-extensions": extension_index(with_tasks),
        "x-mcp-deprecated": {
            **SEP_2577,
            "deprecatedDefinitions": sorted(applied["definitions"]),
            "deprecatedProperties": sorted(applied["properties"]),
            "recoveredFrom": (
                "schema.ts. typescript-json-schema drops JSDoc tags, so the "
                "upstream schema.json carries no deprecation information; these "
                "flags come from the TypeScript source (tools/deprecations.py)."
            ),
        },
    }
    return document


def task_methods(tasks: Source, rename: dict, def_names) -> list[dict]:
    specs = [
        {
            "name": "tasks/get",
            "request": "GetTaskRequest",
            "response": "GetTaskResult",
            "tags": ["tasks"],
            "summary": "Poll a task's status and, once terminal, its result.",
            "docs": "https://modelcontextprotocol.io/extensions/tasks/overview",
            "gated": True,
            "params": {"taskId": {"description": "The `taskId` from the `CreateTaskResult` that seeded this task."}},
            "notes": (
                "A task in `input_required` status carries the outstanding "
                "`inputRequests` here; answer them with `tasks/update`, not by "
                "retrying the original request. Honour `pollIntervalMs`."
            ),
        },
        {
            "name": "tasks/update",
            "request": "UpdateTaskRequest",
            "response": "UpdateTaskResult",
            "tags": ["tasks"],
            "summary": "Supply input a running task asked for.",
            "docs": "https://modelcontextprotocol.io/extensions/tasks/overview",
            "gated": True,
            "params": {
                "taskId": {"description": "The task to supply input to."},
                "inputResponses": {
                    "description": "One entry per key of the `inputRequests` map the task reported through `tasks/get`."
                },
            },
            "notes": "Keys in `inputResponses` MUST match the keys of the `inputRequests` map returned by `tasks/get`.",
        },
        {
            "name": "tasks/cancel",
            "request": "CancelTaskRequest",
            "response": "CancelTaskResult",
            "tags": ["tasks"],
            "summary": "Request cancellation of a running task.",
            "docs": "https://modelcontextprotocol.io/extensions/tasks/overview",
            "gated": True,
            "params": {"taskId": {"description": "The task to cancel."}},
        },
    ]
    methods = []
    for spec in specs:
        method = build_method(tasks, spec, rename, def_names, param_overrides=spec.get("params"))
        method["x-mcp-extension"] = "io.modelcontextprotocol/tasks"
        method["x-mcp-http-headers"] = {
            "Mcp-Method": "method",
            "Mcp-Name": "params.taskId",
        }
        method["x-mcp-note"] = (
            "Requires the `io.modelcontextprotocol/tasks` extension in this "
            "request's `clientCapabilities`; a server MUST answer -32021 "
            "otherwise. Over Streamable HTTP the `Mcp-Name` header carries "
            "`params.taskId` so intermediaries can route to the instance "
            "holding the task's state."
        )
        methods.append(method)
    return methods


def read_sources() -> dict:
    sources = json.loads((SCHEMA / "SOURCES.json").read_text())
    return {
        "note": "Upstream schemas these documents are generated from, pinned by commit.",
        "pins": sources["pins"],
        "regenerate": "python3 tools/vendor.py && python3 tools/gen_openrpc.py && python3 tools/validate.py",
    }


def extension_index(with_tasks: bool) -> dict:
    described = [
        identifier
        for identifier, info in EXTENSION_PROSE.items()
        if info["addsMethods"] and with_tasks
    ]
    return {
        "note": (
            "Official extensions, for orientation only. Extensions are opt-in and "
            "negotiated per request through "
            "`_meta['io.modelcontextprotocol/clientCapabilities'].extensions` and, "
            "server-side, `server/discover` -> `capabilities.extensions`. "
            + (
                "The Tasks extension is described as methods in this document."
                if with_tasks
                else "No extension is described in this document; see the extensions variant."
            )
        ),
        "describedHere": described,
        "catalogue": EXTENSION_PROSE,
    }


def core_description(with_tasks: bool) -> str:
    scope = (
        "the ten client-to-server methods of the core protocol plus the Tasks "
        "extension's three"
        if with_tasks
        else "the ten client-to-server methods of the core protocol"
    )
    text = f"""OpenRPC description of the Model Context Protocol server surface, revision {MCP_VERSION}.

Generated from the pinned upstream JSON Schema (see `x-mcp-sources`) by `tools/gen_openrpc.py`; do not edit by hand.

## Scope

An OpenRPC document describes one JSON-RPC service. This one describes the **MCP server**: {scope}, and the one notification a client sends it. That is the whole of what a client may call, because this revision removed server-initiated JSON-RPC requests entirely.

Two things a server emits are therefore *not* methods here, and are recorded in root extensions instead:

* **Server-to-client notifications** — `x-mcp-server-notifications`.
* **Sampling, roots, and elicitation** — `x-mcp-input-requests`. These are no longer JSON-RPC methods at all: they are bare objects (no `jsonrpc`, no `id`) inside the `inputRequests` map of an `input_required` result, answered by retrying the original request with `inputResponses` and the opaque `requestState`.

Their schemas are still present in `components.schemas`, so a generator can emit the client-side types from this document.

## What changed in {MCP_VERSION}

* **No `initialize`.** The protocol is stateless. Every request re-declares its protocol version and capabilities in `params._meta` under `io.modelcontextprotocol/*`, and a server MUST NOT infer them from earlier requests. `server/discover` is optional for clients and MUST be implemented by servers.
* **No server-initiated requests.** Replaced by the Multi Round-Trip Requests pattern described above.
* **`resources/subscribe`/`resources/unsubscribe` are gone**, replaced by `subscriptions/listen`.
* **`logging/setLevel` is gone**, replaced by the already-deprecated `io.modelcontextprotocol/logLevel` request `_meta` field.
* **`ping` and `notifications/initialized` are gone.**
* **Results are polymorphic**, discriminated by `resultType`; an absent `resultType` means `"complete"` and identifies a pre-{MCP_VERSION} server.
* **New error codes** `-32020`, `-32021`, `-32022`; `-32002` (resource not found) is retired in favour of `-32602`.
* **Sampling, roots, and logging are deprecated** (SEP-2577), leaving elicitation as the only non-deprecated client feature. They remain in the specification for at least twelve months, so they are described here in full and flagged, not omitted. The upstream `schema.json` carries none of these markers — they are recovered from `schema.ts`; see `x-mcp-deprecated`.

## Conventions

* MCP sends params by name, so every method declares `paramStructure: "by-name"` and each member of the upstream `params` object is one named parameter. `_meta` is listed first.
* Each method's result union is taken verbatim from the upstream `*ResultResponse` wrapper, so a method that can answer `input_required` says so and one that cannot does not.
* Schema Objects are draft-07 as OpenRPC requires; see `x-mcp-json-schema` for the downgrade applied to the 2020-12 originals.
* Transport detail that OpenRPC cannot express lives in `x-mcp-transports`; the deprecated 2024-11-05 HTTP+SSE transport is deliberately out of scope.
"""
    if with_tasks:
        text += """
## Extensions

Of the official extensions, only **Tasks** adds methods to the MCP server surface, so only Tasks is described here as methods (`tasks/get`, `tasks/update`, `tasks/cancel`, tagged `tasks`) together with the `CreateTaskResult` alternative on `tools/call` — the one method that supports task augmentation. Its `notifications/tasks` notification is listed in `x-mcp-server-notifications`.

**MCP Apps**, the two **authorization** extensions, **Server Card**, and **Skills over MCP** add no methods to this service; `x-mcp-extensions` records what each one does contribute and where its own specification lives. The MCP Apps `ui/*` messages are a separate service and have their own document.
"""
    return text


# --------------------------------------------------------------------------- #
# MCP Apps document
# --------------------------------------------------------------------------- #

APPS_METHODS = [
    ("ui/initialize", "McpUiInitializeRequest", "McpUiInitializeResult", "view-to-host", "Handshake: the view announces its capabilities and receives the host's."),
    ("ui/notifications/initialized", "McpUiInitializedNotification", None, "view-to-host", "The view has finished initialising. The host MUST NOT send it anything before this arrives."),
    ("ui/open-link", "McpUiOpenLinkRequest", "McpUiOpenLinkResult", "view-to-host", "Ask the host to open an external URL."),
    ("ui/message", "McpUiMessageRequest", "McpUiMessageResult", "view-to-host", "Send message content to the host's chat interface."),
    ("ui/request-display-mode", "McpUiRequestDisplayModeRequest", "McpUiRequestDisplayModeResult", "view-to-host", "Ask the host to change display mode. The host returns the resulting mode whether or not it changed."),
    ("ui/update-model-context", "McpUiUpdateModelContextRequest", None, "view-to-host", "Contribute context for the model's future turns."),
    ("ui/download-file", "McpUiDownloadFileRequest", "McpUiDownloadFileResult", "view-to-host", "Ask the host to save a file, since sandboxed iframes cannot download directly."),
    ("ui/notifications/size-changed", "McpUiSizeChangedNotification", None, "view-to-host", "The view's content size changed; hosts using flexible dimensions resize the frame."),
    ("ui/notifications/request-teardown", "McpUiRequestTeardownNotification", None, "view-to-host", "The view asks to be torn down. The host decides, and if it agrees sends `ui/resource-teardown`."),
    ("ui/resource-teardown", "McpUiResourceTeardownRequest", "McpUiResourceTeardownResult", "host-to-view", "The host is about to unmount the view, which may clean up before responding."),
    ("ui/notifications/tool-input", "McpUiToolInputNotification", None, "host-to-view", "Complete tool arguments. Sent at most once, and required before a tool result."),
    ("ui/notifications/tool-input-partial", "McpUiToolInputPartialNotification", None, "host-to-view", "Streaming partial tool arguments, sent zero or more times before the complete ones."),
    ("ui/notifications/tool-result", "McpUiToolResultNotification", None, "host-to-view", "The tool call's result."),
    ("ui/notifications/tool-cancelled", "McpUiToolCancelledNotification", None, "host-to-view", "The tool call was cancelled."),
    ("ui/notifications/host-context-changed", "McpUiHostContextChangedNotification", None, "host-to-view", "Host context changed — theme, display mode, locale, and similar."),
    ("ui/notifications/sandbox-proxy-ready", "McpUiSandboxProxyReadyNotification", None, "sandbox", "Sandbox proxy to host: ready to receive the HTML resource."),
    ("ui/notifications/sandbox-resource-ready", "McpUiSandboxResourceReadyNotification", None, "sandbox", "Host to sandbox proxy: the HTML resource to load."),
]

APPS_UNTYPED_RESULT = {
    "ui/update-model-context": (
        "The 2026-01-26 schema defines no result type for this request, though "
        "the specification's sequence diagrams show a response. Modelled as an "
        "unconstrained object."
    )
}


def apps_document() -> dict:
    apps = Source(SCHEMA / f"ext-apps/{APPS_VERSION}/schema.json")
    def_names = set(apps.defs)
    schemas = {name: downgrade(node, {}, def_names) for name, node in sorted(apps.defs.items())}

    methods = []
    for name, request_def, result_def, direction, summary in APPS_METHODS:
        declared = apps.method_name(request_def)
        if declared != name:
            raise SystemExit(f"apps table says {name!r} but schema says {declared!r}")
        params_object = apps.params_object(request_def)
        params = content_descriptors(params_object, {}, def_names, first=())

        method: dict = {"name": name, "summary": summary}
        upstream = apps.description(request_def)
        if upstream:
            method["description"] = upstream
        method["tags"] = [{"$ref": f"#/components/tags/{direction}"}]
        method["paramStructure"] = "by-name"
        method["params"] = params

        if result_def:
            schema, description = split_description({"$ref": f"#/$defs/{result_def}"}, {}, def_names)
            method["result"] = {"name": result_def[0].lower() + result_def[1:], "required": True, "schema": schema}
        elif name in APPS_UNTYPED_RESULT:
            method["result"] = {
                "name": "result",
                "required": True,
                "description": APPS_UNTYPED_RESULT[name],
                "schema": {"type": "object"},
            }
        else:
            method["x-mcp-message-type"] = "notification"
            method["x-mcp-note"] = "No `result` is declared, which marks this method as notification-only in OpenRPC."

        method["x-mcp-apps-direction"] = direction
        methods.append(method)

    return {
        "$schema": "https://meta.open-rpc.org/",
        "openrpc": OPENRPC_VERSION,
        "info": {
            "title": "MCP Apps — Host and View Surface",
            "version": DOC_VERSION,
            "description": apps_description(),
            "license": LICENSE,
            "x-mcp-extension": "io.modelcontextprotocol/ui",
            "x-mcp-extension-version": APPS_VERSION,
        },
        "externalDocs": {
            "description": "MCP Apps extension specification",
            "url": "https://modelcontextprotocol.io/extensions/apps/overview",
        },
        "methods": methods,
        "components": {
            "tags": {
                name: {"name": name, "description": TAGS[name]}
                for name in ("view-to-host", "host-to-view", "sandbox")
            },
            "schemas": schemas,
        },
        "x-mcp-json-schema": {
            "sourceDialect": "https://json-schema.org/draft/2020-12/schema",
            "documentDialect": DRAFT07,
            "note": "See the core document's `x-mcp-json-schema` for the downgrade applied.",
        },
        "x-mcp-sources": read_sources(),
        "x-mcp-transports": {
            "postmessage": {
                "framing": "JSON-RPC 2.0 messages over `window.postMessage` between the host page and the embedded view's iframe.",
                "note": "A sandbox proxy may sit between host and view; it forwards every method except `ui/notifications/sandbox-*`.",
                "docs": "https://modelcontextprotocol.io/extensions/apps/overview",
            }
        },
    }


def apps_description() -> str:
    return f"""OpenRPC description of the MCP Apps (`io.modelcontextprotocol/ui`) host <-> embedded view surface, extension revision {APPS_VERSION}.

Generated from the pinned upstream schema (see `x-mcp-sources`) by `tools/gen_openrpc.py`; do not edit by hand.

## Why this is a separate document

MCP Apps adds **no methods to the MCP server surface** — the extension specification says so explicitly. What it adds there is metadata: `_meta.ui` on tool and resource declarations. The `ui/*` methods below are a different JSON-RPC service entirely, spoken over `window.postMessage` between a host application and the app it embeds in an iframe. They never travel over an MCP transport and no MCP server implements them.

## Direction

Unlike the MCP server surface, this one has requests in both directions, so a single-direction document would omit real methods. Every method is tagged and carries `x-mcp-apps-direction`:

* `view-to-host` — the embedded view calls the host.
* `host-to-view` — the host calls the view.
* `sandbox` — reserved messages exchanged with the sandbox proxy that web hosts interpose.

Consumers generating one side's handlers should filter on that tag.

## Notes

* This extension versions independently of the core protocol: {APPS_VERSION}, not {MCP_VERSION}.
* `ui/download-file` and `ui/notifications/request-teardown` exist in the published schema but are not described in the {APPS_VERSION} prose specification; their direction here is taken from the upstream type documentation.
* The upstream schema inlines its types rather than cross-referencing them, so `components.schemas` mirrors that shape.
"""


def main() -> int:
    core = Source(SCHEMA / f"mcp/{MCP_VERSION}/schema.json")
    examples_dir = SCHEMA / f"mcp/{MCP_VERSION}/examples"

    targets = [
        (SPEC / MCP_VERSION / "mcp-server.openrpc.json", lambda: core_document(core, examples_dir, False)),
        (SPEC / MCP_VERSION / "mcp-server-extensions.openrpc.json", lambda: core_document(core, examples_dir, True)),
        (SPEC / f"apps-{APPS_VERSION}" / "mcp-apps-ui.openrpc.json", apps_document),
    ]
    for path, builder in targets:
        print(f"generating {path.relative_to(ROOT)}")
        document = builder()
        size = write_json(path, document)
        print(f"  {len(document['methods'])} methods, {len(document['components']['schemas'])} schemas, {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
