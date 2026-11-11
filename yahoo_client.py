#!/usr/bin/env python3
"""Shared Yahoo Mail client for the custom.yahoo-mail connector.

Stdlib only (imaplib / smtplib / poplib / email). One client used by
both the CLI (bin/yahoo-mail) and the MCP server (bin/yahoo-mail-mcp).

WHY PROTOCOLS, NOT AN API (surveyed live 2026-11-11): Yahoo has no
public Mail API. The proprietary JSON-RPC API (mail.yahooapis.com) is
retired, and Yahoo's OAuth host issues mail-r / mail-w scopes only
under a signed Commercial Access Agreement — not self-serve. The
sanctioned consumer route is the standard mail protocols with a Yahoo
APP PASSWORD (Account Security -> Create app password; 16 characters;
the main account password is rejected by design):

  IMAP  imap.mail.yahoo.com:993  SSL   (read + mailbox management)
  SMTP  smtp.mail.yahoo.com:465  SSL   (send; 587 STARTTLS fallback)
  POP3  pop.mail.yahoo.com:995   SSL   (inbox-only legacy reads)

Standard protocols are also the replaceable/migratable choice: any
mail client on earth speaks them, so this connector can be swapped
without touching the mailbox.

Credentials (universal — this client is published for anyone to use):
resolved in order, first hit wins —
  1. env vars YAHOO_MAIL_ADDRESS and YAHOO_MAIL_APP_PASSWORD
  2. a .env file: $YAHOO_MAIL_ENV_FILE, else ./.env, else
     ~/.config/yahoo-mail/.env  (plain KEY=VALUE lines)
  3. OPTIONAL fleet integration: if the Muse dynamic-credentials
     helper is importable (it exists only inside Muse's agent
     runtime), fall back to Doppler project "yahoo-mail", config
     "dev". Public users never take this branch; it is inert without
     the helper module.
Values live in memory only: never printed, never logged, never
written to disk by this code. creds_status reports presence + source
only. Placeholder values count as NOT configured.
"""

import email
import email.policy
import hashlib
import imaplib
import os
import poplib
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.header import decode_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

IMAP_HOST, IMAP_PORT = "imap.mail.yahoo.com", 993
SMTP_HOST, SMTP_PORT = "smtp.mail.yahoo.com", 465
POP3_HOST, POP3_PORT = "pop.mail.yahoo.com", 995
DOPPLER_SECRETS_URL = ("https://api.doppler.com/v3/configs/config/secrets"
                       "?project=yahoo-mail&config=dev")
DOWNLOAD_DIR = os.path.expanduser("~/workspace/yahoo-mail-downloads")
MAX_BODY_CHARS = 200_000
MAX_RAW_CHARS = 500_000

SECRET_NAMES = ("YAHOO_MAIL_ADDRESS", "YAHOO_MAIL_APP_PASSWORD")
PLACEHOLDER_MARKERS = ("PLACEHOLDER", "REPLACE_WITH")

ENDPOINT_CENSUS = [
    {"name": "creds_status", "kind": "meta", "write": False, "about": "Credential presence + source (never values)"},
    {"name": "capabilities", "kind": "imap", "write": False, "about": "IMAP server capabilities + identity"},
    {"name": "folders_list", "kind": "imap", "write": False, "about": "All folders with flags/special-use"},
    {"name": "folder_status", "kind": "imap", "write": False, "about": "Message counts, unseen, UIDNEXT for a folder"},
    {"name": "search", "kind": "imap", "write": False, "about": "IMAP SEARCH by from/to/subject/text/date/flags; returns UIDs + summaries"},
    {"name": "list_messages", "kind": "imap", "write": False, "about": "Latest N message summaries in a folder"},
    {"name": "get_message", "kind": "imap", "write": False, "about": "One message parsed: headers, text/html bodies, attachment list"},
    {"name": "get_message_raw", "kind": "imap", "write": False, "about": "One message as raw RFC822 (size-capped)"},
    {"name": "get_attachment", "kind": "imap", "write": False, "about": "Download one attachment to ~/workspace/yahoo-mail-downloads (returns path + sha256, never inlines bytes)"},
    {"name": "quota", "kind": "imap", "write": False, "about": "Mailbox quota usage/limit"},
    {"name": "namespace", "kind": "imap", "write": False, "about": "IMAP namespaces"},
    {"name": "pop3_stat", "kind": "pop3", "write": False, "about": "POP3 inbox count + total size"},
    {"name": "pop3_list", "kind": "pop3", "write": False, "about": "POP3 message numbers + sizes"},
    {"name": "pop3_read", "kind": "pop3", "write": False, "about": "Read one POP3 message, parsed like get_message"},
    {"name": "send", "kind": "smtp", "write": True, "about": "Send a new message (to/cc/bcc, text + optional HTML, file attachments). WRITE."},
    {"name": "reply", "kind": "smtp", "write": True, "about": "Reply to a message with correct In-Reply-To/References threading. WRITE."},
    {"name": "forward", "kind": "smtp", "write": True, "about": "Forward a message as a new message with the original attached as .eml. WRITE."},
    {"name": "save_draft", "kind": "imap", "write": True, "about": "Save a composed message into the Drafts folder (APPEND, \\Draft flag). WRITE."},
    {"name": "append_message", "kind": "imap", "write": True, "about": "APPEND a raw RFC822 message to any folder with optional flags. WRITE."},
    {"name": "mark", "kind": "imap", "write": True, "about": "Set/clear flags (seen, flagged, answered, deleted, draft) on one or more UIDs. WRITE."},
    {"name": "move", "kind": "imap", "write": True, "about": "Move messages to another folder (MOVE extension, copy+delete fallback). WRITE."},
    {"name": "copy", "kind": "imap", "write": True, "about": "Copy messages to another folder. WRITE."},
    {"name": "delete", "kind": "imap", "write": True, "about": "Delete messages: moves them to Trash (permanent only from Trash itself). WRITE."},
    {"name": "expunge", "kind": "imap", "write": True, "about": "Expunge \\Deleted messages in a folder. WRITE (irreversible)."},
    {"name": "empty_trash", "kind": "imap", "write": True, "about": "Permanently delete everything in Trash. WRITE (irreversible)."},
    {"name": "create_folder", "kind": "imap", "write": True, "about": "Create a folder. WRITE."},
    {"name": "delete_folder", "kind": "imap", "write": True, "about": "Delete a folder (must be empty of subfolders Yahoo cares about). WRITE."},
    {"name": "rename_folder", "kind": "imap", "write": True, "about": "Rename a folder. WRITE."},
]


class YahooError(Exception):
    """Any credential, transport, or protocol failure, one line."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

_CREDS = None


def _is_placeholder(value):
    return any(marker in value for marker in PLACEHOLDER_MARKERS)


def _dotenv_values():
    """Read the first .env file found; returns {} when none exists."""
    candidates = []
    if os.environ.get("YAHOO_MAIL_ENV_FILE"):
        candidates.append(os.environ["YAHOO_MAIL_ENV_FILE"])
    candidates.append(os.path.join(os.getcwd(), ".env"))
    candidates.append(os.path.expanduser("~/.config/yahoo-mail/.env"))
    for path in candidates:
        if os.path.isfile(path):
            values = {}
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip("'\"")
            return values
    return {}


def _fetch_doppler():
    """Optional fleet fallback. Returns {} when the Muse helper
    module is not present (the normal case for public users)."""
    helper_dir = "/opt/muse/skills/custom-connector-setup/bin"
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    try:
        from dynamic_credentials import add_surrogate_to_request, \
            read_json_response
    except Exception:
        return {}

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise YahooError("Doppler request redirected; refusing")

    request = urllib.request.Request(DOPPLER_SECRETS_URL, method="GET")
    add_surrogate_to_request(request, "custom.doppler",
                             allowed_hosts=["api.doppler.com"])
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            payload = read_json_response(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise YahooError(f"Doppler read failed (HTTP {e.code}): {detail}")
    secrets = payload.get("secrets", {}) if isinstance(payload, dict) else {}
    values = {}
    for name, entry in secrets.items():
        if isinstance(entry, dict):
            values[name] = (entry.get("computed") or entry.get("raw")
                            or entry.get("value"))
        else:
            values[name] = entry
    return values


def credentials():
    """Return (address, app_password, source) or raise YahooError."""
    global _CREDS
    if _CREDS is not None:
        return _CREDS
    try:
        doppler_values = _fetch_doppler()
    except YahooError:
        doppler_values = {}
    for source, values in (("environment", os.environ),
                           (".env file", _dotenv_values()),
                           ("doppler:yahoo-mail/dev", doppler_values)):
        address = values.get("YAHOO_MAIL_ADDRESS") or ""
        password = values.get("YAHOO_MAIL_APP_PASSWORD") or ""
        if address and password and not _is_placeholder(address) \
                and not _is_placeholder(password):
            _CREDS = (address, password, source)
            return _CREDS
    raise YahooError(
        "No Yahoo Mail credential configured. Setup (2 minutes): "
        "(1) Sign in at https://login.yahoo.com/account/security, "
        "under 'Other ways to sign in' choose 'App passwords' -> "
        "'Generate app password', name it anything (e.g. 'AI mail "
        "connector') and copy the 16-character code — your main "
        "password will NOT work, Yahoo rejects it for IMAP/SMTP by "
        "design. (2) Set YAHOO_MAIL_ADDRESS=<your full Yahoo address> "
        "and YAHOO_MAIL_APP_PASSWORD=<the app password> as env vars "
        "or in a .env file. See README.md.")


def _source_status(values):
    status = {}
    for name in SECRET_NAMES:
        value = values.get(name)
        status[name] = {"present": bool(value),
                        "placeholder": bool(value)
                        and _is_placeholder(value)}
    status["usable"] = all(v["present"] and not v["placeholder"]
                           for v in status.values())
    return status


def creds_status():
    out = {"sources": {}}
    active = "none"
    for label, values in (("environment", os.environ),
                          (".env file", _dotenv_values())):
        status = _source_status(values)
        out["sources"][label] = status
        if status["usable"] and active == "none":
            active = label
    try:
        status = _source_status(_fetch_doppler())
        out["sources"]["doppler:yahoo-mail/dev"] = status
        if status["usable"] and active == "none":
            active = "doppler:yahoo-mail/dev"
    except YahooError as e:
        out["sources"]["doppler:yahoo-mail/dev"] = {"error": str(e)}
    out["configured"] = active != "none"
    out["active_source"] = active
    return out


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def _imap():
    address, password, _ = credentials()
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT,
                                 ssl_context=ssl.create_default_context())
        conn.login(address, password)
    except imaplib.IMAP4.error as e:
        raise YahooError(f"IMAP login failed: {e}. If the password is "
                         f"the main account password, that is the cause "
                         f"— Yahoo requires an app password.")
    return conn


def _pop3():
    address, password, _ = credentials()
    try:
        conn = poplib.POP3_SSL(POP3_HOST, POP3_PORT,
                               context=ssl.create_default_context())
        conn.user(address)
        conn.pass_(password)
    except poplib.error_proto as e:
        raise YahooError(f"POP3 login failed: {e}")
    return conn


def _select(conn, folder, readonly=True):
    typ, data = conn.select(_quote(folder), readonly=readonly)
    if typ != "OK":
        raise YahooError(f"Cannot open folder {folder!r}: "
                         f"{data[0] if data else typ}")
    return int(data[0])


def _quote(folder):
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _uids_arg(uids):
    if isinstance(uids, (list, tuple)):
        return ",".join(str(int(u)) for u in uids)
    return str(int(uids))


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _decode(value):
    if value is None:
        return ""
    parts = []
    for chunk, charset in decode_header(str(value)):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", "replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _addr_list(value):
    if not value:
        return []
    return [{"name": _decode(name), "address": addr}
            for name, addr in getaddresses([str(value)]) if addr]


def _parse_message(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    headers = {}
    for key in ("From", "To", "Cc", "Subject", "Date", "Message-ID",
                "Reply-To"):
        if msg.get(key) is not None:
            headers[key.lower().replace("-", "_")] = str(msg.get(key))
    parsed = {
        "subject": _decode(msg.get("Subject", "")),
        "from": _addr_list(msg.get("From")),
        "to": _addr_list(msg.get("To")),
        "cc": _addr_list(msg.get("Cc")),
        "date": headers.get("date"),
        "message_id": headers.get("message_id"),
        "size_bytes": len(raw_bytes),
    }
    try:
        dt = parsedate_to_datetime(str(msg.get("Date")))
        parsed["date_iso"] = dt.isoformat()
    except Exception:
        parsed["date_iso"] = None
    text_body, html_body = None, None
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp or part.get_filename():
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    "index": len(attachments),
                    "filename": _decode(part.get_filename() or "unnamed"),
                    "content_type": ctype,
                    "size_bytes": len(payload)})
            elif ctype == "text/plain" and text_body is None:
                text_body = _part_text(part)
            elif ctype == "text/html" and html_body is None:
                html_body = _part_text(part)
    else:
        if msg.get_content_type() == "text/html":
            html_body = _part_text(msg)
        else:
            text_body = _part_text(msg)
    parsed["text"] = (text_body or "")[:MAX_BODY_CHARS]
    parsed["html"] = (html_body or "")[:MAX_BODY_CHARS] or None
    parsed["attachments"] = attachments
    return parsed


def _part_text(part):
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode(part.get_content_charset() or "utf-8",
                                  "replace")
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8", "replace")


def _envelope_summary(uid, data_bytes):
    msg = email.message_from_bytes(data_bytes,
                                   policy=email.policy.compat32)
    return {
        "uid": int(uid),
        "subject": _decode(msg.get("Subject", "")),
        "from": _addr_list(msg.get("From")),
        "to": _addr_list(msg.get("To")),
        "date": str(msg.get("Date") or "") or None,
        "message_id": str(msg.get("Message-ID") or "") or None,
    }


# --------------------------------------------------------------------------
# IMAP reads
# --------------------------------------------------------------------------

def capabilities():
    conn = _imap()
    try:
        caps = sorted(conn.capabilities)
        return {"host": IMAP_HOST, "capabilities": caps,
                "move_supported": "MOVE" in caps,
                "quota_supported": "QUOTA" in caps,
                "idle_supported": "IDLE" in caps}
    finally:
        _logout(conn)


def _logout(conn):
    try:
        conn.logout()
    except Exception:
        pass


def folders_list():
    conn = _imap()
    try:
        typ, rows = conn.list()
        if typ != "OK":
            raise YahooError(f"LIST failed: {typ}")
        folders = []
        for row in rows or []:
            line = row.decode("utf-8", "replace") \
                if isinstance(row, bytes) else str(row)
            m = re.match(r"\(([^)]*)\)\s+\"[^\"]*\"\s+(.*)$", line)
            if not m:
                continue
            flags = [f for f in m.group(1).split() if f]
            name = m.group(2).strip().strip('"')
            special = [f for f in flags if f.startswith("\\")
                       and f.lower() not in ("\\haschildren",
                                             "\\hasnochildren",
                                             "\\noselect")]
            folders.append({"name": name, "flags": flags,
                            "special_use": special[0] if special else None})
        return {"count": len(folders), "folders": folders}
    finally:
        _logout(conn)


def folder_status(folder="INBOX"):
    conn = _imap()
    try:
        out = {"folder": folder}
        typ, data = conn.status(_quote(folder),
                                "(MESSAGES RECENT UNSEEN UIDNEXT UIDVALIDITY)")
        if typ == "OK" and data and data[0]:
            line = data[0].decode() if isinstance(data[0], bytes) \
                else str(data[0])
            for key in ("MESSAGES", "RECENT", "UNSEEN", "UIDNEXT",
                        "UIDVALIDITY"):
                m = re.search(key + r" (\d+)", line)
                if m:
                    out[key.lower()] = int(m.group(1))
        return out
    finally:
        _logout(conn)


def _search_uids(conn, folder, criteria):
    _select(conn, folder, readonly=True)
    typ, data = conn.uid("SEARCH", None, criteria)
    if typ != "OK":
        raise YahooError(f"SEARCH failed in {folder}: {criteria}")
    return [int(u) for u in (data[0] or b"").split()]


def _criteria(args):
    parts = []
    if args.get("unseen"):
        parts.append("UNSEEN")
    if args.get("seen"):
        parts.append("SEEN")
    if args.get("flagged"):
        parts.append("FLAGGED")
    if args.get("from_"):
        parts += ["FROM", _imap_quote_str(args["from_"])]
    if args.get("to"):
        parts += ["TO", _imap_quote_str(args["to"])]
    if args.get("subject"):
        parts += ["SUBJECT", _imap_quote_str(args["subject"])]
    if args.get("text"):
        parts += ["TEXT", _imap_quote_str(args["text"])]
    if args.get("since"):
        parts += ["SINCE", args["since"]]
    if args.get("before"):
        parts += ["BEFORE", args["before"]]
    raw = args.get("raw")
    if raw:
        return raw
    return " ".join(parts) if parts else "ALL"


def _imap_quote_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _summaries(conn, folder, uids, limit):
    picked = sorted(uids, reverse=True)[:limit]
    results = []
    for uid in picked:
        typ, data = conn.uid(
            "FETCH", str(uid),
            "(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
            "(FROM TO SUBJECT DATE MESSAGE-ID)])")
        if typ != "OK" or not data:
            continue
        size, flags = None, []
        header_bytes = b""
        for item in data:
            if isinstance(item, tuple):
                meta = item[0].decode("utf-8", "replace")
                m = re.search(r"RFC822.SIZE (\d+)", meta)
                if m:
                    size = int(m.group(1))
                m = re.search(r"FLAGS \(([^)]*)\)", meta)
                if m:
                    flags = m.group(1).split()
                header_bytes = item[1] or b""
            elif isinstance(item, bytes) and b"Subject" in item:
                header_bytes = item
        entry = _envelope_summary(uid, header_bytes)
        entry["size_bytes"] = size
        entry["flags"] = flags
        results.append(entry)
    return results


def search(folder="INBOX", limit=20, **filters):
    conn = _imap()
    try:
        criteria = _criteria(filters)
        uids = _search_uids(conn, folder, criteria)
        results = _summaries(conn, folder, uids, limit)
        return {"folder": folder, "criteria": criteria,
                "total_matches": len(uids),
                "returned": len(results), "messages": results}
    finally:
        _logout(conn)


def list_messages(folder="INBOX", limit=20):
    return search(folder=folder, limit=limit)


def _fetch_raw(conn, folder, uid):
    _select(conn, folder, readonly=True)
    typ, data = conn.uid("FETCH", str(int(uid)), "(RFC822)")
    if typ != "OK" or not data:
        raise YahooError(f"Message UID {uid} not found in {folder}")
    for item in data:
        if isinstance(item, tuple) and item[1]:
            return item[1]
    raise YahooError(f"Message UID {uid} in {folder} has no body")


def get_message(folder, uid):
    conn = _imap()
    try:
        raw = _fetch_raw(conn, folder, uid)
        parsed = _parse_message(raw)
        parsed["folder"] = folder
        parsed["uid"] = int(uid)
        return parsed
    finally:
        _logout(conn)


def get_message_raw(folder, uid):
    conn = _imap()
    try:
        raw = _fetch_raw(conn, folder, uid)
        return {"folder": folder, "uid": int(uid),
                "size_bytes": len(raw),
                "raw": raw.decode("utf-8", "replace")[:MAX_RAW_CHARS]}
    finally:
        _logout(conn)


def get_attachment(folder, uid, index):
    conn = _imap()
    try:
        raw = _fetch_raw(conn, folder, uid)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        atts = []
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp or part.get_filename():
                atts.append(part)
        if index >= len(atts):
            raise YahooError(f"Message has {len(atts)} attachment(s); "
                             f"index {index} does not exist")
        part = atts[index]
        payload = part.get_payload(decode=True) or b""
        filename = _decode(part.get_filename() or f"attachment-{index}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "attachment"
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        path = os.path.join(DOWNLOAD_DIR, f"{int(uid)}-{index}-{safe}")
        with open(path, "wb") as fh:
            fh.write(payload)
        return {"folder": folder, "uid": int(uid), "index": index,
                "filename": filename,
                "content_type": part.get_content_type(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "saved_to": path}
    finally:
        _logout(conn)


def quota():
    conn = _imap()
    try:
        typ, data = conn.getquotaroot("INBOX")
        quotas = []
        if typ == "OK":
            for row in data[1] if len(data) > 1 else []:
                line = row.decode() if isinstance(row, bytes) else str(row)
                m = re.search(r"\(([^)]*)\)", line)
                quotas.append(m.group(1) if m else line)
        return {"quotaroot": str(data[0]) if data else None,
                "quotas": quotas,
                "note": "Values are (resource usage limit) triples in KB "
                        "as the server reports them."}
    finally:
        _logout(conn)


def namespace():
    conn = _imap()
    try:
        typ, data = conn.namespace()
        return {"ok": typ == "OK",
                "namespace": [d.decode("utf-8", "replace")
                              if isinstance(d, bytes) else str(d)
                              for d in (data or [])]}
    finally:
        _logout(conn)


# --------------------------------------------------------------------------
# POP3 reads
# --------------------------------------------------------------------------

def pop3_stat():
    conn = _pop3()
    try:
        count, size = conn.stat()
        return {"messages": count, "total_bytes": size}
    finally:
        conn.quit()


def pop3_list(limit=50):
    conn = _pop3()
    try:
        _, rows, _ = conn.list()
        items = []
        for row in rows[:limit]:
            num, size = row.decode().split()
            items.append({"number": int(num), "size_bytes": int(size)})
        return {"count": len(rows), "returned": len(items), "messages": items}
    finally:
        conn.quit()


def pop3_read(number):
    conn = _pop3()
    try:
        _, lines, _ = conn.retr(int(number))
        raw = b"\r\n".join(lines)
        parsed = _parse_message(raw)
        parsed["pop3_number"] = int(number)
        return parsed
    finally:
        conn.quit()


# --------------------------------------------------------------------------
# Compose + send (SMTP) and drafts (IMAP APPEND)
# --------------------------------------------------------------------------

def _build_message(to, subject, body, cc=None, bcc=None, html=None,
                   attach=None, in_reply_to=None, references=None):
    address, _, _ = credentials()
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = ", ".join(to if isinstance(to, list) else [to])
    if cc:
        msg["Cc"] = ", ".join(cc if isinstance(cc, list) else [cc])
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attach or []:
        if not os.path.isfile(path):
            raise YahooError(f"Attachment file not found: {path}")
        with open(path, "rb") as fh:
            data = fh.read()
        maintype, _, subtype = (
            _guess_type(path)).partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=os.path.basename(path))
    recipients = list(to if isinstance(to, list) else [to])
    for extra in (cc, bcc):
        if extra:
            recipients += list(extra if isinstance(extra, list) else [extra])
    return msg, recipients


def _guess_type(path):
    import mimetypes
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _smtp_send(msg, recipients):
    address, password, _ = credentials()
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context,
                          timeout=60) as server:
        server.login(address, password)
        refused = server.send_message(msg, to_addrs=recipients)
    return {"sent": True, "from": address, "recipients": recipients,
            "refused": {k: list(v) for k, v in (refused or {}).items()},
            "subject": str(msg.get("Subject", ""))}


def send(to, subject, body, cc=None, bcc=None, html=None, attach=None):
    msg, recipients = _build_message(to, subject, body, cc=cc, bcc=bcc,
                                     html=html, attach=attach)
    return _smtp_send(msg, recipients)


def _original(folder, uid):
    conn = _imap()
    try:
        raw = _fetch_raw(conn, folder, uid)
    finally:
        _logout(conn)
    return email.message_from_bytes(raw, policy=email.policy.default), raw


def reply(folder, uid, body, reply_all=False, attach=None):
    orig, raw = _original(folder, uid)
    address, _, _ = credentials()
    to = str(orig.get("Reply-To") or orig.get("From") or "")
    cc = None
    if reply_all:
        pool = []
        for header in ("To", "Cc"):
            pool += [a for _, a in getaddresses([str(orig.get(header)
                                                     or "")])]
        cc = [a for a in pool if a and a.lower() != address.lower()]
    subject = str(orig.get("Subject") or "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    mid = str(orig.get("Message-ID") or "")
    refs = str(orig.get("References") or "")
    references = (refs + " " + mid).strip() if mid else refs
    quoted = "\n\n----- Original message -----\n" + \
        (_parse_message(raw)["text"] or "")
    msg, recipients = _build_message(
        [a for _, a in getaddresses([to])] or [to], subject,
        (body or "") + quoted, cc=cc, attach=attach,
        in_reply_to=mid or None, references=references or None)
    return _smtp_send(msg, recipients)


def forward(folder, uid, to, body=None):
    orig, raw = _original(folder, uid)
    address, _, _ = credentials()
    subject = str(orig.get("Subject") or "")
    if not subject.lower().startswith("fwd:"):
        subject = "Fwd: " + subject
    intro = (body or "") + \
        "\n\n----- Forwarded message -----\n" + \
        f"From: {orig.get('From', '')}\nDate: {orig.get('Date', '')}\n" + \
        f"Subject: {orig.get('Subject', '')}\n" + \
        (_parse_message(raw)["text"] or "")
    msg, recipients = _build_message(to, subject, intro)
    msg.add_attachment(raw, maintype="message", subtype="rfc822",
                       filename="forwarded-message.eml")
    return _smtp_send(msg, recipients)


def _drafts_folder(conn):
    typ, rows = conn.list()
    for row in rows or []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) \
            else str(row)
        if "\\Drafts" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
    return "Drafts"


def save_draft(to, subject, body, cc=None, html=None):
    msg, _ = _build_message(to, subject, body, cc=cc, html=html)
    conn = _imap()
    try:
        folder = _drafts_folder(conn)
        typ, data = conn.append(_quote(folder), "\\Draft",
                                None, msg.as_bytes())
        if typ != "OK":
            raise YahooError(f"APPEND to {folder} failed: {data}")
        return {"saved": True, "folder": folder, "subject": subject}
    finally:
        _logout(conn)


def append_message(folder, raw_rfc822, flags=None):
    conn = _imap()
    try:
        flag_str = ""
        if flags:
            flag_str = "(" + " ".join(
                f if f.startswith("\\") else "\\" + f for f in flags) + ")"
        typ, data = conn.append(_quote(folder), flag_str or None, None,
                                raw_rfc822.encode("utf-8", "replace"))
        if typ != "OK":
            raise YahooError(f"APPEND failed: {data}")
        return {"appended": True, "folder": folder}
    finally:
        _logout(conn)


# --------------------------------------------------------------------------
# IMAP writes
# --------------------------------------------------------------------------

_FLAG_MAP = {"seen": "\\Seen", "unseen": "\\Seen", "flagged": "\\Flagged",
             "answered": "\\Answered", "deleted": "\\Deleted",
             "draft": "\\Draft"}


def mark(folder, uids, flag, on=True):
    key = flag.lower().lstrip("\\")
    if key not in _FLAG_MAP:
        raise YahooError(f"Unknown flag {flag!r}; use one of "
                         f"{sorted(set(_FLAG_MAP))}")
    conn = _imap()
    try:
        _select(conn, folder, readonly=False)
        mode = "+FLAGS" if on else "-FLAGS"
        typ, data = conn.uid("STORE", _uids_arg(uids), mode,
                             f"({_FLAG_MAP[key]})")
        if typ != "OK":
            raise YahooError(f"STORE failed: {data}")
        return {"folder": folder, "uids": str(_uids_arg(uids)),
                "flag": _FLAG_MAP[key], "set": on}
    finally:
        _logout(conn)


def _trash_folder(conn):
    typ, rows = conn.list()
    for row in rows or []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) \
            else str(row)
        if "\\Trash" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
    return "Trash"


def move(folder, uids, dest):
    conn = _imap()
    try:
        _select(conn, folder, readonly=False)
        arg = _uids_arg(uids)
        if "MOVE" in conn.capabilities:
            typ, data = conn.uid("MOVE", arg, _quote(dest))
            if typ == "OK":
                return {"moved": True, "from": folder, "to": dest,
                        "uids": arg, "method": "MOVE"}
        typ, data = conn.uid("COPY", arg, _quote(dest))
        if typ != "OK":
            raise YahooError(f"COPY to {dest} failed: {data}")
        conn.uid("STORE", arg, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return {"moved": True, "from": folder, "to": dest, "uids": arg,
                "method": "copy+delete+expunge"}
    finally:
        _logout(conn)


def copy(folder, uids, dest):
    conn = _imap()
    try:
        _select(conn, folder, readonly=False)
        typ, data = conn.uid("COPY", _uids_arg(uids), _quote(dest))
        if typ != "OK":
            raise YahooError(f"COPY failed: {data}")
        return {"copied": True, "from": folder, "to": dest,
                "uids": str(_uids_arg(uids))}
    finally:
        _logout(conn)


def delete(folder, uids):
    conn = _imap()
    try:
        trash = _trash_folder(conn)
        _select(conn, folder, readonly=False)
        arg = _uids_arg(uids)
        if folder.strip('"') == trash:
            conn.uid("STORE", arg, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return {"deleted": True, "folder": folder, "uids": arg,
                    "permanent": True}
        if "MOVE" in conn.capabilities:
            typ, _ = conn.uid("MOVE", arg, _quote(trash))
            if typ == "OK":
                return {"deleted": True, "folder": folder, "uids": arg,
                        "permanent": False, "moved_to": trash}
        typ, data = conn.uid("COPY", arg, _quote(trash))
        if typ != "OK":
            raise YahooError(f"Delete failed (copy to Trash): {data}")
        conn.uid("STORE", arg, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return {"deleted": True, "folder": folder, "uids": arg,
                "permanent": False, "moved_to": trash}
    finally:
        _logout(conn)


def expunge(folder):
    conn = _imap()
    try:
        _select(conn, folder, readonly=False)
        typ, data = conn.expunge()
        return {"expunged": typ == "OK", "folder": folder,
                "expunged_count": len(data or [])}
    finally:
        _logout(conn)


def empty_trash():
    conn = _imap()
    try:
        trash = _trash_folder(conn)
        count = _select(conn, trash, readonly=False)
        if count:
            conn.uid("STORE", "1:*", "+FLAGS", "(\\Deleted)")
            conn.expunge()
        return {"emptied": True, "folder": trash, "messages_removed": count}
    finally:
        _logout(conn)


def create_folder(name):
    conn = _imap()
    try:
        typ, data = conn.create(_quote(name))
        if typ != "OK":
            raise YahooError(f"CREATE failed: {data}")
        return {"created": True, "folder": name}
    finally:
        _logout(conn)


def delete_folder(name):
    conn = _imap()
    try:
        typ, data = conn.delete(_quote(name))
        if typ != "OK":
            raise YahooError(f"DELETE folder failed: {data}")
        return {"deleted": True, "folder": name}
    finally:
        _logout(conn)


def rename_folder(old, new):
    conn = _imap()
    try:
        typ, data = conn.rename(_quote(old), _quote(new))
        if typ != "OK":
            raise YahooError(f"RENAME failed: {data}")
        return {"renamed": True, "from": old, "to": new}
    finally:
        _logout(conn)
