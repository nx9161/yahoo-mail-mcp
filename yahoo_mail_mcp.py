#!/usr/bin/env python3
"""Yahoo Mail MCP server — the full mailbox as MCP tools.

Local stdio transport, JSON-RPC 2.0, standard library only. All logic
is reused from yahoo_client.py (same directory); nothing duplicated.

Talks to Yahoo over IMAP / SMTP / POP3 with a Yahoo app password
(Yahoo has no public Mail API — its proprietary API is retired and the
OAuth mail scopes require a commercial agreement). Credentials come
from env vars YAHOO_MAIL_ADDRESS / YAHOO_MAIL_APP_PASSWORD, or a .env
file. Read tools run plain; every write tool refuses unless the call
carries "write_ok": true.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yahoo_client as ym  # noqa: E402

SERVER_NAME = "yahoo-mail-mcp"
SERVER_VERSION = "1.0.0"

WRITE_OK = {"write_ok": {"type": "boolean",
                         "description": "Must be true to perform any "
                                        "write (send, move, delete, …)"}}
_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_UIDS = {"type": "array", "items": {"type": "integer"},
         "description": "Message UIDs"}


def _schema(props, required=None):
    return {"type": "object", "properties": props,
            "required": required or []}


def _write(props, required=None):
    return _schema({**props, **WRITE_OK},
                   (required or []) + ["write_ok"])


_FOLDER = {"folder": {**_S, "description": "Folder name (default INBOX)"}}
_LIMIT = {"limit": {**_I, "description": "Max results (default 20)"}}

TOOLS = [
    {"name": "ym_creds_status", "description": "Credential presence + active source (never values)",
     "inputSchema": _schema({})},
    {"name": "ym_endpoints", "description": "Full operation census",
     "inputSchema": _schema({})},
    {"name": "ym_capabilities", "description": "IMAP server capabilities",
     "inputSchema": _schema({})},
    {"name": "ym_folders", "description": "List all folders with special-use flags",
     "inputSchema": _schema({})},
    {"name": "ym_folder_status", "description": "Message/unseen counts for a folder",
     "inputSchema": _schema(_FOLDER, [])},
    {"name": "ym_quota", "description": "Mailbox quota usage",
     "inputSchema": _schema({})},
    {"name": "ym_list", "description": "Latest message summaries in a folder",
     "inputSchema": _schema({**_FOLDER, **_LIMIT})},
    {"name": "ym_search", "description": "Search a folder (from/to/subject/text/date/flags or raw IMAP criteria)",
     "inputSchema": _schema({**_FOLDER, **_LIMIT, "from_": _S, "to": _S,
                             "subject": _S, "text": _S, "since": _S,
                             "before": _S, "unseen": _B, "seen": _B,
                             "flagged": _B, "raw": _S})},
    {"name": "ym_read", "description": "Read one message parsed (headers, bodies, attachment list)",
     "inputSchema": _schema({**_FOLDER, "uid": _I}, ["uid"])},
    {"name": "ym_read_raw", "description": "Read one message as raw RFC822 (capped)",
     "inputSchema": _schema({**_FOLDER, "uid": _I}, ["uid"])},
    {"name": "ym_attachment", "description": "Download one attachment to ~/workspace/yahoo-mail-downloads (or ./yahoo-mail-downloads); returns path + sha256",
     "inputSchema": _schema({**_FOLDER, "uid": _I, "index": _I},
                            ["uid", "index"])},
    {"name": "ym_pop3_stat", "description": "POP3 inbox count + size",
     "inputSchema": _schema({})},
    {"name": "ym_pop3_list", "description": "POP3 message numbers + sizes",
     "inputSchema": _schema({"limit": _I})},
    {"name": "ym_pop3_read", "description": "Read one POP3 message, parsed",
     "inputSchema": _schema({"number": _I}, ["number"])},
    {"name": "ym_send", "description": "Send a new message. WRITE.",
     "inputSchema": _write({"to": {"type": "array", "items": _S},
                            "subject": _S, "body": _S,
                            "cc": {"type": "array", "items": _S},
                            "bcc": {"type": "array", "items": _S},
                            "html": _S,
                            "attach": {"type": "array", "items": _S,
                                       "description": "Local file paths"}},
                           ["to", "subject"])},
    {"name": "ym_reply", "description": "Reply to a message with proper threading. WRITE.",
     "inputSchema": _write({**_FOLDER, "uid": _I, "body": _S,
                            "reply_all": _B,
                            "attach": {"type": "array", "items": _S}},
                           ["uid"])},
    {"name": "ym_forward", "description": "Forward a message (original attached as .eml). WRITE.",
     "inputSchema": _write({**_FOLDER, "uid": _I,
                            "to": {"type": "array", "items": _S},
                            "body": _S}, ["uid", "to"])},
    {"name": "ym_save_draft", "description": "Save a composed message to Drafts. WRITE.",
     "inputSchema": _write({"to": {"type": "array", "items": _S},
                            "subject": _S, "body": _S,
                            "cc": {"type": "array", "items": _S},
                            "html": _S}, ["to", "subject"])},
    {"name": "ym_append", "description": "Append a raw RFC822 message to a folder. WRITE.",
     "inputSchema": _write({"folder": _S, "raw": _S,
                            "flags": {"type": "array", "items": _S}},
                           ["folder", "raw"])},
    {"name": "ym_mark", "description": "Set/clear a flag (seen, flagged, answered, deleted, draft) on messages. WRITE.",
     "inputSchema": _write({**_FOLDER, "uids": _UIDS, "flag": _S,
                            "on": _B}, ["uids", "flag"])},
    {"name": "ym_move", "description": "Move messages to another folder. WRITE.",
     "inputSchema": _write({**_FOLDER, "uids": _UIDS, "dest": _S},
                           ["uids", "dest"])},
    {"name": "ym_copy", "description": "Copy messages to another folder. WRITE.",
     "inputSchema": _write({**_FOLDER, "uids": _UIDS, "dest": _S},
                           ["uids", "dest"])},
    {"name": "ym_delete", "description": "Delete messages (to Trash; permanent when already in Trash). WRITE.",
     "inputSchema": _write({**_FOLDER, "uids": _UIDS}, ["uids"])},
    {"name": "ym_expunge", "description": "Expunge \\Deleted messages in a folder. WRITE (irreversible).",
     "inputSchema": _write(_FOLDER)},
    {"name": "ym_empty_trash", "description": "Permanently delete everything in Trash. WRITE (irreversible).",
     "inputSchema": _write({})},
    {"name": "ym_create_folder", "description": "Create a folder. WRITE.",
     "inputSchema": _write({"name": _S}, ["name"])},
    {"name": "ym_delete_folder", "description": "Delete a folder. WRITE.",
     "inputSchema": _write({"name": _S}, ["name"])},
    {"name": "ym_rename_folder", "description": "Rename a folder. WRITE.",
     "inputSchema": _write({"old": _S, "new": _S}, ["old", "new"])},
]


def gate(a):
    if a.get("write_ok") is not True:
        raise ym.YahooError(
            "Refused: write tools require \"write_ok\": true in the "
            "arguments. Nothing was sent or changed.")


F = lambda a: a.get("folder", "INBOX")  # noqa: E731

DISPATCH = {
    "ym_creds_status": lambda a: ym.creds_status(),
    "ym_endpoints": lambda a: {
        "total": len(ym.ENDPOINT_CENSUS),
        "writes": sum(1 for e in ym.ENDPOINT_CENSUS if e["write"]),
        "endpoints": ym.ENDPOINT_CENSUS},
    "ym_capabilities": lambda a: ym.capabilities(),
    "ym_folders": lambda a: ym.folders_list(),
    "ym_folder_status": lambda a: ym.folder_status(F(a)),
    "ym_quota": lambda a: ym.quota(),
    "ym_list": lambda a: ym.list_messages(F(a), a.get("limit", 20)),
    "ym_search": lambda a: ym.search(
        folder=F(a), limit=a.get("limit", 20), from_=a.get("from_"),
        to=a.get("to"), subject=a.get("subject"), text=a.get("text"),
        since=a.get("since"), before=a.get("before"),
        unseen=a.get("unseen"), seen=a.get("seen"),
        flagged=a.get("flagged"), raw=a.get("raw")),
    "ym_read": lambda a: ym.get_message(F(a), a["uid"]),
    "ym_read_raw": lambda a: ym.get_message_raw(F(a), a["uid"]),
    "ym_attachment": lambda a: ym.get_attachment(F(a), a["uid"],
                                                 a["index"]),
    "ym_pop3_stat": lambda a: ym.pop3_stat(),
    "ym_pop3_list": lambda a: ym.pop3_list(a.get("limit", 50)),
    "ym_pop3_read": lambda a: ym.pop3_read(a["number"]),
}


def _gated(fn):
    def handler(a):
        gate(a)
        return fn(a)
    return handler


DISPATCH.update({
    "ym_send": _gated(lambda a: ym.send(
        a["to"], a["subject"], a.get("body", ""), cc=a.get("cc"),
        bcc=a.get("bcc"), html=a.get("html"), attach=a.get("attach"))),
    "ym_reply": _gated(lambda a: ym.reply(
        F(a), a["uid"], a.get("body", ""),
        reply_all=bool(a.get("reply_all")), attach=a.get("attach"))),
    "ym_forward": _gated(lambda a: ym.forward(
        F(a), a["uid"], a["to"], body=a.get("body", ""))),
    "ym_save_draft": _gated(lambda a: ym.save_draft(
        a["to"], a["subject"], a.get("body", ""), cc=a.get("cc"),
        html=a.get("html"))),
    "ym_append": _gated(lambda a: ym.append_message(
        a["folder"], a["raw"], flags=a.get("flags"))),
    "ym_mark": _gated(lambda a: ym.mark(
        F(a), a["uids"], a["flag"], on=a.get("on", True))),
    "ym_move": _gated(lambda a: ym.move(F(a), a["uids"], a["dest"])),
    "ym_copy": _gated(lambda a: ym.copy(F(a), a["uids"], a["dest"])),
    "ym_delete": _gated(lambda a: ym.delete(F(a), a["uids"])),
    "ym_expunge": _gated(lambda a: ym.expunge(F(a))),
    "ym_empty_trash": _gated(lambda a: ym.empty_trash()),
    "ym_create_folder": _gated(lambda a: ym.create_folder(a["name"])),
    "ym_delete_folder": _gated(lambda a: ym.delete_folder(a["name"])),
    "ym_rename_folder": _gated(lambda a: ym.rename_folder(a["old"],
                                                          a["new"])),
})


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(mid, payload):
    send({"jsonrpc": "2.0", "id": mid, "result": payload})


def handle_tools_call(mid, params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    fn = DISPATCH.get(name)
    if fn is None:
        result(mid, {"content": [{"type": "text",
                                  "text": "Unknown tool: %s" % name}],
                     "isError": True})
        return
    try:
        payload = fn(arguments)
        text = json.dumps(payload, indent=2, ensure_ascii=False,
                          default=str)
        result(mid, {"content": [{"type": "text", "text": text}],
                     "isError": False})
    except ym.YahooError as e:
        result(mid, {"content": [{"type": "text", "text": str(e)}],
                     "isError": True})
    except KeyError as e:
        result(mid, {"content": [{"type": "text",
                                  "text": "Missing argument: %s" % e}],
                     "isError": True})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            params = msg.get("params") or {}
            result(mid, {
                "protocolVersion": params.get("protocolVersion",
                                              "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME,
                               "version": SERVER_VERSION}})
        elif method in ("notifications/initialized",
                        "notifications/cancelled"):
            pass
        elif method == "ping":
            result(mid, {})
        elif method == "tools/list":
            result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            handle_tools_call(mid, msg.get("params") or {})


if __name__ == "__main__":
    main()
