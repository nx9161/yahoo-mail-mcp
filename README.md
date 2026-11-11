# yahoo-mail-mcp

**Give any AI agent full control of a Yahoo Mail account — reading, searching, sending, organizing — through a CLI and an MCP server. Zero dependencies (Python standard library only).**

## Why this exists

Yahoo Mail has **no public API**. Its proprietary Mail API was retired years ago, and Yahoo's OAuth mail scopes (`mail-r` / `mail-w`) are only issued under a signed Commercial Access Agreement — there is no self-serve developer signup like Gmail's or Outlook's.

What Yahoo *does* support, for every account, is the standard mail protocols with an **app password**:

| Protocol | Server | Port | Used for |
|---|---|---|---|
| IMAP | `imap.mail.yahoo.com` | 993 (SSL) | Reading + folders, flags, move/copy/delete, drafts, quota |
| SMTP | `smtp.mail.yahoo.com` | 465 (SSL) | Sending, replies, forwards |
| POP3 | `pop.mail.yahoo.com` | 995 (SSL) | Legacy inbox reads |

This project wraps those three protocols in one shared client (`yahoo_client.py`) and exposes it two ways, so humans and agents use exactly the same surface:

- **CLI** — `yahoo-mail …` (28 commands, JSON output)
- **MCP server** — `yahoo-mail-mcp` (28 tools over stdio, for Claude, Cursor, and any MCP client)

Because it speaks standard protocols, the mailbox stays portable: nothing here locks you in, and any mail app can take over at any time.

## Quick start (about 2 minutes)

### 1. Generate a Yahoo app password

Your normal Yahoo password will **not** work — Yahoo rejects it for IMAP/SMTP by design. You need the free app password instead:

1. Sign in at **https://login.yahoo.com/account/security**
2. Find **"Other ways to sign in"** → **"App passwords"** → **"Generate app password"** (Yahoo's own help page calls this *Create app password* under *External connections*).
3. Name it anything, e.g. `AI mail connector`, and click **Generate**.
4. Copy the **16-character** password. Yahoo shows it once.

> If the App passwords option is missing: turn on two-step verification first (same Security page), and set up the account in a browser you've signed in from before — Yahoo sometimes hides the option in brand-new or private sessions. Also make sure IMAP is enabled: Yahoo Mail → Settings → More Settings → Mailboxes → IMAP access.

### 2. Give the connector your credentials

Either environment variables:

```bash
export YAHOO_MAIL_ADDRESS="you@yahoo.com"
export YAHOO_MAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"   # the app password
```

…or a `.env` file (copy `.env.example`): in the project folder, the current folder, or `~/.config/yahoo-mail/.env`.

Check it worked:

```bash
python3 yahoo_mail_cli.py creds-status
# -> "configured": true   (values are never printed)
```

### 3. Use it

**As an MCP server for an AI agent** — see the client configs below.

**As a CLI:**

```bash
python3 yahoo_mail_cli.py folders
python3 yahoo_mail_cli.py list --folder INBOX --limit 10
python3 yahoo_mail_cli.py search --unseen --from boss@example.com
python3 yahoo_mail_cli.py read 12345
python3 yahoo_mail_cli.py send --to a@b.com --subject "Hi" --body "Hello" --write-ok
```

Or install it properly (adds `yahoo-mail` and `yahoo-mail-mcp` commands):

```bash
pip install .
```

## Connect it to your AI platform

The MCP server is a plain stdio server: `python3 /path/to/yahoo-mail-mcp/yahoo_mail_mcp.py`, credentials from the environment of that process.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yahoo-mail": {
      "command": "python3",
      "args": ["/path/to/yahoo-mail-mcp/yahoo_mail_mcp.py"],
      "env": {
        "YAHOO_MAIL_ADDRESS": "you@yahoo.com",
        "YAHOO_MAIL_APP_PASSWORD": "xxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add yahoo-mail \
  -e YAHOO_MAIL_ADDRESS=you@yahoo.com \
  -e YAHOO_MAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx \
  -- python3 /path/to/yahoo-mail-mcp/yahoo_mail_mcp.py
```

**Cursor / VS Code / other MCP clients** — same shape: a stdio server whose `command` is `python3`, `args` is the path to `yahoo_mail_mcp.py`, and `env` carries the two variables above. If you installed with pip, the command can simply be `yahoo-mail-mcp` with no args.

Then ask your agent things like *"Check my Yahoo inbox for anything from my bank"*, *"Move all the newsletters to the Newsletters folder"*, or *"Reply to the last email from Sara and say I'll send the file tomorrow"*.

## What it can do

28 operations (14 reads, 14 writes):

- **Read:** credential status, server capabilities, folder list, folder status, quota, namespaces, search (from/to/subject/text/date/flags or raw IMAP criteria), latest-messages list, read a message (parsed: headers, text + HTML bodies, attachment list), read raw RFC822, download an attachment to disk (returns path + SHA-256, never inlines bytes), POP3 stat/list/read.
- **Write:** send (cc/bcc/HTML/attachments), reply (correct threading headers, reply-all supported), forward (original attached as `.eml`), save draft, append a raw message to any folder, mark flags (seen/flagged/answered/deleted/draft, on or off), move, copy, delete (to Trash; permanent only from Trash itself), expunge, empty trash, create/delete/rename folders.

Run `yahoo-mail endpoints` or the `ym_endpoints` tool for the machine-readable census.

## Safety model

An agent with mailbox write access is powerful, so writes are **gated**:

- CLI writes refuse unless you pass `--write-ok`; MCP writes refuse unless the tool call includes `"write_ok": true`. The idea: the flag is set per action, by whoever approved that exact action — it's a speed bump against accidents and prompt-injected agents, not a sandbox.
- Credentials are read from the environment or a `.env` file, held in memory only, and **never printed or logged** (`creds-status` reports presence and source, never values).
- Delete is two-stage everywhere except inside Trash itself: deleting moves mail to Trash; only deleting *from* Trash, `expunge`, and `empty-trash` are permanent.
- Attachment bytes never travel through JSON output — downloads land on disk and you get a path and hash.

Use a dedicated app password for this connector: if anything ever goes wrong, revoke just that app password in Yahoo Account Security and the connector is locked out while your account stays untouched.

## Limits & notes

- Yahoo caps SMTP sending at roughly **500 emails/day, 100 recipients per message**; hourly bursts can trigger a temporary block. This is a mailbox client, not a bulk sender.
- POP3 sees the **Inbox only** and doesn't sync changes back — it's included for completeness; prefer IMAP.
- Yahoo folder names are localized on some accounts; the client finds Drafts/Trash by their IMAP special-use flags rather than hard-coding English names.
- Message bodies are capped in JSON output (200 KB text) and raw output at 500 KB; full fidelity is always available through the attachment/download path and your regular mail app.
- Yahoo Japan accounts (`@yahoo.co.jp`, `@ymail.ne.jp`) run on a separate system and are **not** supported.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `LOGIN failed` / authentication error | You're using the main Yahoo password. Generate an **app password** (step 1). Also check the address is the full `you@yahoo.com`. |
| IMAP connects but folders won't open | IMAP access is off. Enable it: Yahoo Mail → Settings → More Settings → Mailboxes → IMAP. |
| App passwords option missing | Enable two-step verification first; use a browser/session Yahoo recognizes (not private mode, not a brand-new device). |
| SMTP blocked after several sends | Yahoo's hourly/daily send limits — wait and retry later. |
| `creds-status` says not configured | The env vars aren't set in the process that runs the connector — for MCP clients, put them in the server's `env` block, not your shell. |

## Contributing

Issues and PRs welcome. The codebase is deliberately small and dependency-free: one client module, a CLI, and an MCP server that reuses it. Please keep it that way — no third-party packages.

## License

MIT — see [LICENSE](LICENSE).

*Not affiliated with Yahoo Inc. "Yahoo" and "Yahoo Mail" are trademarks of their respective owners. This is an independent client for the standard IMAP/SMTP/POP3 protocols Yahoo documents for third-party mail apps.*
