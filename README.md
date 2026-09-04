# mcp-openrpc-spec

[OpenRPC](https://spec.open-rpc.org/) descriptions of the
[Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28),
revision **2026-07-28**.

MCP is a JSON-RPC 2.0 protocol but ships its interface definition as a
TypeScript file. These documents restate that interface in OpenRPC, so the usual
JSON-RPC tooling — clients and servers generators, documentation renderers,
request validators, mock servers — can consume it directly.

Everything under `spec/` is generated. The upstream schemas under `schema/` are
vendored and pinned by commit, so a regeneration from the same checkout is
byte-identical.

```
make            # vendor upstream schemas, generate spec/, validate
make check      # verify the vendored schemas and the generated documents
```

## The documents

| Document | Describes |
| --- | --- |
| `spec/2026-07-28/mcp-server.openrpc.json` | The core MCP server surface: 10 methods + 1 client-sent notification |
| `spec/2026-07-28/mcp-server-extensions.openrpc.json` | The same, plus every official extension that adds to that surface — which is the Tasks extension alone |
| `spec/apps-2026-01-26/mcp-apps-ui.openrpc.json` | The MCP Apps host ↔ embedded view postMessage surface: a different JSON-RPC service, versioned separately |

## What 2026-07-28 changed

This revision is not an incremental release, and the shape of these documents
follows from it:

- **No `initialize`.** The protocol is stateless. Every request re-declares its
  protocol version and capabilities in `params._meta` under
  `io.modelcontextprotocol/*`; a server MUST NOT infer them from earlier
  requests. `server/discover` replaces the handshake and is optional for
  clients.
- **No server-initiated JSON-RPC requests.** Sampling, roots, and elicitation
  are no longer methods. A server returns an `input_required` result whose
  `inputRequests` map holds bare request objects — no `jsonrpc`, no `id` — and
  the client answers by *retrying the original request* with a matching
  `inputResponses` map and the opaque `requestState`. This is the Multi
  Round-Trip Requests (MRTR) pattern.
- **`resources/subscribe` / `resources/unsubscribe` are gone**, replaced by a
  long-lived `subscriptions/listen` request whose response stream carries the
  notifications the client opted in to.
- **`logging/setLevel` is gone**, replaced by the already-deprecated
  `io.modelcontextprotocol/logLevel` request `_meta` field.
- **`ping` and `notifications/initialized` are gone.**
- **Results are polymorphic**, discriminated by `resultType`. An absent
  `resultType` identifies a pre-2026-07-28 server and means `"complete"`.
- **New error codes** `-32020` (header mismatch), `-32021` (missing client
  capability), `-32022` (unsupported protocol version). `-32002` (resource not
  found) is retired in favour of `-32602` and never reused.
- **Sampling, roots, and logging are deprecated** (SEP-2577), leaving
  elicitation as the only client feature that is not. They stay in the
  specification for at least twelve months, so these documents describe them in
  full and flag them rather than dropping them.

## Modelling decisions

An OpenRPC document describes exactly one JSON-RPC service, and these decisions
are all downstream of taking that seriously.

**Server surface only.** `methods` holds what a client may call on an MCP
server, plus `notifications/cancelled`, the one notification a client sends it.
Nothing else belongs to that service:

- Server-emitted notifications are indexed in `x-mcp-server-notifications`.
- Sampling, roots, and elicitation are indexed in `x-mcp-input-requests`.

Both keep their payload schemas in `components.schemas`, so a code generator can
still emit the client-side types from these documents — they are simply not
presented as callable methods, because in this revision they are not.

**Params are flattened and by-name.** MCP always sends a params *object*, so
each member becomes one named OpenRPC parameter and every method declares
`paramStructure: "by-name"`. `_meta` is listed first, then required parameters,
then optional ones.

**Result unions are taken verbatim from upstream.** Each method's result is
whatever its `*ResultResponse` wrapper says, so `tools/call` and `prompts/get`
show `anyOf [CallToolResult, InputRequiredResult]` while `tools/list` shows a
single shape. Nothing is invented and nothing is flattened to a happy path.

**JSON Schema draft-07.** OpenRPC 1.x requires it; the upstream schemas are
2020-12. The downgrade is mechanical and recorded in each document's
`x-mcp-json-schema`: `$defs` references rewritten to `#/components/schemas/...`,
subschema `$schema`/`$id` dropped, and `$ref` with sibling keywords wrapped as
`allOf: [{$ref}]` so descriptions are not silently discarded. No other
2020-12-only keyword is used upstream — verified, not assumed.

**`openrpc: "1.3.2"`.** The prose specification at spec.open-rpc.org is numbered
1.4.x, but the canonical meta-schema published at
[meta.open-rpc.org](https://meta.open-rpc.org/) enumerates document versions only
up to 1.3.2. Declaring 1.3.2 keeps these documents validatable against that
meta-schema; nothing here uses a field introduced later.

**Deprecations are recovered from the TypeScript source.** `schema.json` is
generated by typescript-json-schema, which keeps descriptions but discards JSDoc
tags — so upstream's machine-readable schema says nothing at all about the
SEP-2577 deprecations. `tools/deprecations.py` parses `schema.ts` for those
markers (21 of them, all cross-checked against a tag count) and the generator
applies them to definitions, properties, and methods; `x-mcp-deprecated`
summarises what was flagged. `tools/validate.py` fails if any marker stops
reaching the output.

**Extensions.** Of the official extensions, only **Tasks** adds methods to the
MCP server surface, so only Tasks appears as methods — `tasks/get`,
`tasks/update`, `tasks/cancel`, plus `CreateTaskResult` as an alternative result
on `tools/call`, the one method that supports task augmentation. **MCP Apps**,
the two **authorization** extensions, **Server Card**, and **Skills over MCP**
add nothing callable to an MCP server; `x-mcp-extensions` records what each one
contributes and where its own specification lives. MCP Apps' `ui/*` messages are
a genuine JSON-RPC service, just not this one, so they get their own document.

**Transports.** OpenRPC's `servers` array can only express a URL, so the
Streamable HTTP entry is a template with placeholder variables and the full
binding detail — required headers, status codes, SSE framing, what this revision
removed — lives in `x-mcp-transports`, which also covers stdio. The deprecated
2024-11-05 HTTP+SSE transport is deliberately out of scope.

## Known limits of the OpenRPC encoding

These are places where the protocol says more than OpenRPC can:

- **Streaming.** `subscriptions/listen` does not return promptly; its response
  is a stream of notifications terminated by a result. OpenRPC has no vocabulary
  for that, so the declared result describes only the terminal message and
  `x-mcp-streaming` describes the rest.
- **Error payloads.** OpenRPC's Error Object admits `code`, `message`, and a
  `data` *value* — there is no slot for a schema of `data`, and it forbids `x-`
  fields. The structured payloads of `-32021` and `-32022` therefore live in
  `components.schemas` and are indexed by `x-mcp-error-codes`.
- **Direction.** The MCP documents need no direction marker because everything
  in them travels client → server. The MCP Apps surface has requests in both
  directions, so each of its methods carries a tag and `x-mcp-apps-direction`;
  generate one side by filtering on it.

## Repository layout

```
Makefile
schema/                       vendored upstream sources, pinned by commit
  SOURCES.json                URLs, commit SHAs, sha256 of every vendored file
  mcp/2026-07-28/             core schema, schema.ts, and the example files
  ext-tasks/2026-07-28/       Tasks extension schema
  ext-apps/2026-01-26/        MCP Apps schema
  vendor/                     OpenRPC meta-schema, so validation runs offline
spec/                         generated OpenRPC documents
tools/
  vendor.py                   fetch and pin upstream sources
  openrpc_lib.py              dialect downgrade, params flattening, def merging
  deprecations.py             recover @deprecated markers from schema.ts
  gen_openrpc.py              method tables, prose, document assembly
  validate.py                 meta-schema, refs, dialect, coverage, examples
```

## Validation

`tools/validate.py` needs `jsonschema` and runs offline. It checks that each
document validates against the vendored OpenRPC meta-schema; that every internal
`$ref` resolves; that no 2020-12 construct survived and no `$ref` carries
siblings draft-07 would ignore; that the method list matches the upstream
`ClientRequest`/`ClientNotification` unions **exactly**, so a method cannot be
invented or missed; that method and parameter names are unique and results are
declared required; that every upstream `@deprecated` marker still reaches the
output; and that every embedded example validates against the parameter and
result schemas the document itself declares.

## Updating to a later MCP revision

1. Bump the pinned SHAs and version constants at the top of `tools/vendor.py`,
   and `MCP_VERSION` / `APPS_VERSION` in `tools/gen_openrpc.py`.
2. `make` — the method tables in `tools/gen_openrpc.py` are asserted against the
   schema's own `method` constants, so a renamed or removed method fails the
   build rather than passing silently. `tools/validate.py` fails on a method
   added upstream and not yet in the table.

## Sources

- MCP specification: <https://modelcontextprotocol.io/specification/2026-07-28>
- MCP schema: <https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/schema/2026-07-28>
- OpenRPC: <https://spec.open-rpc.org/>
- JSON-RPC 2.0: <https://www.jsonrpc.org/specification>

The vendored upstream schemas are MIT licensed by the Model Context Protocol
authors; `schema/SOURCES.json` records exactly which commits they came from.
