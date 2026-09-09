#!/usr/bin/env python3
"""A deterministic stand-in for `gbrain serve` — test fixture, not a client.

Speaks just enough MCP over stdio for scripts/gbrain_history.py: `initialize`,
`notifications/initialized` and `tools/call` for the two read tools the
adapter uses. The brain it serves is a JSON file of invented pages, so no
private history is ever needed to test the adapter.

    fake_gbrain.py --brain fixture.json [--mode ok] [--cap 100] [--log calls.log]
                   [--cursor inclusive|exclusive]

`--cursor` picks how `updated_after` filters. gbrain 0.48.3.0 answers
*inclusively* (every batch after the first repeats the boundary row) even
though the schema documents a strict `>`, so inclusive is the default here and
`exclusive` rehearses the documented reading.

`--mode` picks a failure to rehearse:

    ok               behave
    ignore-scope     answer list_pages with every source's rows, `source_id`
                     notwithstanding (a server that does not honour the scope)
    no-start         die before the handshake (gbrain missing/broken)
    handshake-error  answer `initialize` with a JSON-RPC error
    handshake-bad-info answer `initialize` with malformed serverInfo metadata
    private-stderr   emit a synthetic private value on stderr, then exit
    private-server-info put a synthetic private value in serverInfo.version
    pages-error      answer list_pages with isError
    pages-garbage    answer list_pages with text that is not JSON
    pages-not-list   answer list_pages with a JSON object instead of rows
    federated-error  answer only the `source_id: __all__` collision audit with
                     isError (a scoped read works, proving nothing about the
                     rest of the brain)
    federated-tie    answer the `__all__` audit with a full batch of rows tied
                     on one timestamp, so the audit walk stops early
    die-after-pages  exit after the first list_pages reply
    die-in-history   serve the page index, then exit on the first get_versions
    versions-error   answer every get_versions with isError
    versions-not-list  answer get_versions with a JSON object instead of rows
    hang             never answer anything

Every tool call is appended to `--log` so a test can prove the adapter only
ever reads.
"""
import argparse
import json
import sys


def reply(message_id, payload, is_error=False):
    result = {"content": [{"type": "text", "text": payload}]}
    if is_error:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def list_pages(brain, arguments, cap, cursor="inclusive", scoped=True):
    rows = sorted(brain.get("pages", []), key=lambda row: (str(row.get("updated_at")),
                                                           str(row.get("slug"))))
    source_id = arguments.get("source_id")
    if scoped and source_id and source_id != "__all__":
        rows = [row for row in rows if row.get("source_id") == source_id]
    after = arguments.get("updated_after")
    if after:
        rows = [row for row in rows
                if (str(row.get("updated_at")) >= after if cursor == "inclusive"
                    else str(row.get("updated_at")) > after)]
    limit = min(int(arguments.get("limit") or 50), cap)
    # Garbage rows ride along in the first batch, ahead of the real ones, the
    # way a half-broken row would come off the wire.
    head = list(brain.get("malformed_rows", [])) if after is None else []
    return head + rows[:limit]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brain", required=True)
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--cap", type=int, default=100)
    parser.add_argument("--cursor", default="inclusive",
                        choices=["inclusive", "exclusive"])
    parser.add_argument("--log", default=None)
    args, _ignored = parser.parse_known_args()

    if args.mode == "no-start":
        sys.stderr.write("fake gbrain: no brain configured\n")
        return 1
    if args.mode == "private-stderr":
        sys.stderr.write("private/slugs/customer-alpha: SUPER-SECRET-REVISION-BODY\n")
        return 1
    with open(args.brain, encoding="utf-8") as handle:
        brain = json.load(handle)

    def record(name, arguments):
        if args.log:
            with open(args.log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"tool": name, "arguments": arguments}) + "\n")

    def send(message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, message_id = message.get("method"), message.get("id")
        if args.mode == "hang":
            continue
        if method == "initialize":
            if args.mode == "handshake-error":
                send({"jsonrpc": "2.0", "id": message_id,
                      "error": {"code": -32600, "message": "surface not supported"}})
                continue
            if args.mode == "handshake-bad-info":
                send({"jsonrpc": "2.0", "id": message_id, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": []}})
                continue
            version = ("BACKEND_PRIVATE_VERSION" if args.mode == "private-server-info"
                       else "0.0.0-test")
            send({"jsonrpc": "2.0", "id": message_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake-gbrain", "version": version}}})
            continue
        if method != "tools/call":
            continue
        params = message.get("params") or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        record(name, arguments)
        if name == "list_pages":
            federated = arguments.get("source_id") == "__all__"
            if args.mode == "federated-error" and federated:
                send(reply(message_id, json.dumps({"error": "internal",
                                                   "message": "JSONRPC_BACKEND_PRIVATE_MESSAGE"}),
                           is_error=True))
                continue
            if args.mode == "federated-tie" and federated:
                tie = "2026-06-05T00:00:00.000Z"
                send(reply(message_id, json.dumps(
                    [{"slug": "tied/%04d" % i, "source_id": "brain", "updated_at": tie}
                     for i in range(int(arguments.get("limit") or 50))])))
                continue
            if args.mode == "pages-error":
                send(reply(message_id, json.dumps({"error": "internal", "message": "brain is down"}),
                           is_error=True))
                continue
            if args.mode == "pages-garbage":
                send(reply(message_id, "<html>proxy error</html>"))
                continue
            if args.mode == "pages-not-list":
                send(reply(message_id, json.dumps({"pages": []})))
                continue
            send(reply(message_id, json.dumps(
                list_pages(brain, arguments, args.cap, args.cursor,
                           scoped=args.mode != "ignore-scope"))))
            if args.mode == "die-after-pages":
                return 0
            continue
        if name == "get_versions":
            if args.mode == "die-in-history":
                return 0
            if args.mode == "versions-error":
                send(reply(message_id, json.dumps({"error": "invalid_params",
                                                   "message": "no such page"}), is_error=True))
                continue
            if args.mode == "versions-not-list":
                send(reply(message_id, json.dumps({"versions": []})))
                continue
            rows = brain.get("versions", {}).get(arguments.get("slug"), [])
            send(reply(message_id, json.dumps(rows)))
            continue
        send(reply(message_id, json.dumps({"error": "unknown_tool",
                                           "message": "Unknown tool: %s" % name}), is_error=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
