#!/usr/bin/env python3
"""Yahoo Mail CLI — full mailbox control over IMAP / SMTP / POP3.

Yahoo has no public Mail API (retired; OAuth mail scopes need a
commercial agreement), so this speaks the standard protocols with a
Yahoo app password — the same access any mail app uses:

  export YAHOO_MAIL_ADDRESS=you@yahoo.com
  export YAHOO_MAIL_APP_PASSWORD=<16-char app password>
  yahoo-mail folders

Reads are plain commands. Every write (send, reply, forward, drafts,
flags, move, copy, delete, expunge, folder changes, empty trash)
refuses without --write-ok. Output is JSON throughout.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yahoo_client as ym  # noqa: E402


def emit(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def gate(label, args):
    if not getattr(args, "write_ok", False):
        fail(f"'{label}' is a WRITE. It only runs with --write-ok, "
             f"which asserts the account owner's explicit approval for "
             f"this exact action. Nothing was sent or changed.")


def uids(text):
    return [int(x) for x in str(text).split(",") if x.strip()]


def build_parser():
    p = argparse.ArgumentParser(
        prog="yahoo-mail",
        description="Yahoo Mail over IMAP/SMTP/POP3 (app password auth)")
    sub = p.add_subparsers(dest="command", required=True)

    def cmd(name, helptext):
        return sub.add_parser(name, help=helptext)

    def write_flag(parser):
        parser.add_argument("--write-ok", action="store_true")

    q = cmd("creds-status", "credential presence/source (never values)")
    q.set_defaults(fn=lambda a: emit(ym.creds_status()))
    q = cmd("endpoints", "full operation census")
    q.set_defaults(fn=lambda a: emit({
        "total": len(ym.ENDPOINT_CENSUS),
        "writes": sum(1 for e in ym.ENDPOINT_CENSUS if e["write"]),
        "endpoints": ym.ENDPOINT_CENSUS}))
    q = cmd("capabilities", "IMAP capabilities")
    q.set_defaults(fn=lambda a: emit(ym.capabilities()))
    q = cmd("folders", "list folders")
    q.set_defaults(fn=lambda a: emit(ym.folders_list()))
    q = cmd("status", "folder status")
    q.add_argument("--folder", default="INBOX")
    q.set_defaults(fn=lambda a: emit(ym.folder_status(a.folder)))
    q = cmd("quota", "mailbox quota")
    q.set_defaults(fn=lambda a: emit(ym.quota()))
    q = cmd("namespace", "IMAP namespaces")
    q.set_defaults(fn=lambda a: emit(ym.namespace()))

    q = cmd("list", "latest messages in a folder")
    q.add_argument("--folder", default="INBOX")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(fn=lambda a: emit(ym.list_messages(a.folder, a.limit)))

    q = cmd("search", "IMAP SEARCH with filters")
    q.add_argument("--folder", default="INBOX")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--from", dest="from_")
    q.add_argument("--to")
    q.add_argument("--subject")
    q.add_argument("--text")
    q.add_argument("--since", help="IMAP date, e.g. 01-Nov-2026")
    q.add_argument("--before", help="IMAP date, e.g. 01-Nov-2026")
    q.add_argument("--unseen", action="store_true")
    q.add_argument("--seen", action="store_true")
    q.add_argument("--flagged", action="store_true")
    q.add_argument("--raw", help="raw IMAP SEARCH criteria string")
    q.set_defaults(fn=lambda a: emit(ym.search(
        folder=a.folder, limit=a.limit, from_=a.from_, to=a.to,
        subject=a.subject, text=a.text, since=a.since, before=a.before,
        unseen=a.unseen, seen=a.seen, flagged=a.flagged, raw=a.raw)))

    q = cmd("read", "read one message, parsed")
    q.add_argument("uid", type=int)
    q.add_argument("--folder", default="INBOX")
    q.set_defaults(fn=lambda a: emit(ym.get_message(a.folder, a.uid)))
    q = cmd("read-raw", "read one message, raw RFC822")
    q.add_argument("uid", type=int)
    q.add_argument("--folder", default="INBOX")
    q.set_defaults(fn=lambda a: emit(ym.get_message_raw(a.folder, a.uid)))
    q = cmd("attachment", "download an attachment to disk")
    q.add_argument("uid", type=int)
    q.add_argument("index", type=int)
    q.add_argument("--folder", default="INBOX")
    q.set_defaults(fn=lambda a: emit(
        ym.get_attachment(a.folder, a.uid, a.index)))

    q = cmd("pop3-stat", "POP3 inbox count/size")
    q.set_defaults(fn=lambda a: emit(ym.pop3_stat()))
    q = cmd("pop3-list", "POP3 message list")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=lambda a: emit(ym.pop3_list(a.limit)))
    q = cmd("pop3-read", "read a POP3 message")
    q.add_argument("number", type=int)
    q.set_defaults(fn=lambda a: emit(ym.pop3_read(a.number)))

    # ---------------- writes ----------------
    q = cmd("send", "send a new message")
    q.add_argument("--to", required=True, help="comma-separated allowed")
    q.add_argument("--subject", required=True)
    q.add_argument("--body", default="")
    q.add_argument("--body-file", help="read body text from a file")
    q.add_argument("--html")
    q.add_argument("--cc")
    q.add_argument("--bcc")
    q.add_argument("--attach", action="append", default=[])
    write_flag(q)

    def do_send(a):
        gate("send", a)
        body = a.body
        if a.body_file:
            with open(a.body_file, encoding="utf-8") as fh:
                body = fh.read()
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()] \
            if v else None  # noqa: E731
        emit(ym.send(split(a.to), a.subject, body, cc=split(a.cc),
                     bcc=split(a.bcc), html=a.html, attach=a.attach))
    q.set_defaults(fn=do_send)

    q = cmd("reply", "reply to a message")
    q.add_argument("uid", type=int)
    q.add_argument("--folder", default="INBOX")
    q.add_argument("--body", default="")
    q.add_argument("--body-file")
    q.add_argument("--all", action="store_true", dest="reply_all")
    q.add_argument("--attach", action="append", default=[])
    write_flag(q)

    def do_reply(a):
        gate("reply", a)
        body = a.body
        if a.body_file:
            with open(a.body_file, encoding="utf-8") as fh:
                body = fh.read()
        emit(ym.reply(a.folder, a.uid, body, reply_all=a.reply_all,
                      attach=a.attach))
    q.set_defaults(fn=do_reply)

    q = cmd("forward", "forward a message")
    q.add_argument("uid", type=int)
    q.add_argument("--folder", default="INBOX")
    q.add_argument("--to", required=True)
    q.add_argument("--body", default="")
    write_flag(q)

    def do_forward(a):
        gate("forward", a)
        emit(ym.forward(a.folder, a.uid,
                        [x.strip() for x in a.to.split(",") if x.strip()],
                        body=a.body))
    q.set_defaults(fn=do_forward)

    q = cmd("save-draft", "save a draft into the Drafts folder")
    q.add_argument("--to", required=True)
    q.add_argument("--subject", required=True)
    q.add_argument("--body", default="")
    q.add_argument("--body-file")
    q.add_argument("--cc")
    write_flag(q)

    def do_draft(a):
        gate("save-draft", a)
        body = a.body
        if a.body_file:
            with open(a.body_file, encoding="utf-8") as fh:
                body = fh.read()
        emit(ym.save_draft([x.strip() for x in a.to.split(",")
                            if x.strip()], a.subject, body,
                           cc=[x.strip() for x in a.cc.split(",")
                               if x.strip()] if a.cc else None))
    q.set_defaults(fn=do_draft)

    q = cmd("append", "append a raw .eml file to a folder")
    q.add_argument("folder")
    q.add_argument("file", help="path to an RFC822 .eml file")
    q.add_argument("--flag", action="append", default=[])
    write_flag(q)

    def do_append(a):
        gate("append", a)
        with open(a.file, encoding="utf-8", errors="replace") as fh:
            emit(ym.append_message(a.folder, fh.read(),
                                   flags=a.flag or None))
    q.set_defaults(fn=do_append)

    q = cmd("mark", "set or clear a flag on messages")
    q.add_argument("uids", help="comma-separated UIDs")
    q.add_argument("--folder", default="INBOX")
    q.add_argument("--flag", required=True,
                   help="seen|flagged|answered|deleted|draft")
    q.add_argument("--off", action="store_true", help="clear instead of set")
    write_flag(q)

    def do_mark(a):
        gate("mark", a)
        emit(ym.mark(a.folder, uids(a.uids), a.flag, on=not a.off))
    q.set_defaults(fn=do_mark)

    for name, verb in (("move", "move messages to a folder"),
                       ("copy", "copy messages to a folder")):
        q = cmd(name, verb)
        q.add_argument("uids")
        q.add_argument("--folder", default="INBOX")
        q.add_argument("--to", required=True, dest="dest")
        write_flag(q)

        def make(verb=name):
            def handler(a):
                gate(verb, a)
                fn = ym.move if verb == "move" else ym.copy
                emit(fn(a.folder, uids(a.uids), a.dest))
            return handler
        q.set_defaults(fn=make())

    q = cmd("delete", "delete messages (to Trash; permanent from Trash)")
    q.add_argument("uids")
    q.add_argument("--folder", default="INBOX")
    write_flag(q)

    def do_delete(a):
        gate("delete", a)
        emit(ym.delete(a.folder, uids(a.uids)))
    q.set_defaults(fn=do_delete)

    q = cmd("expunge", "expunge \\Deleted messages in a folder")
    q.add_argument("--folder", default="INBOX")
    write_flag(q)

    def do_expunge(a):
        gate("expunge", a)
        emit(ym.expunge(a.folder))
    q.set_defaults(fn=do_expunge)

    q = cmd("empty-trash", "permanently delete everything in Trash")
    write_flag(q)

    def do_empty(a):
        gate("empty-trash", a)
        emit(ym.empty_trash())
    q.set_defaults(fn=do_empty)

    q = cmd("create-folder", "create a folder")
    q.add_argument("name")
    write_flag(q)

    def do_cf(a):
        gate("create-folder", a)
        emit(ym.create_folder(a.name))
    q.set_defaults(fn=do_cf)

    q = cmd("delete-folder", "delete a folder")
    q.add_argument("name")
    write_flag(q)

    def do_df(a):
        gate("delete-folder", a)
        emit(ym.delete_folder(a.name))
    q.set_defaults(fn=do_df)

    q = cmd("rename-folder", "rename a folder")
    q.add_argument("old")
    q.add_argument("new")
    write_flag(q)

    def do_rf(a):
        gate("rename-folder", a)
        emit(ym.rename_folder(a.old, a.new))
    q.set_defaults(fn=do_rf)

    return p


def main():
    args = build_parser().parse_args()
    try:
        args.fn(args)
    except ym.YahooError as e:
        fail(str(e))


if __name__ == "__main__":
    main()
