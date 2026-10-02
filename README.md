# gmail-mcp

An MCP server that gives an AI agent **read access, draft management, and confirmed
sending for Gmail, one mailbox per process.**

Most multi-account Gmail servers hold every account in a single process. This one does
the opposite: each server instance is configured with exactly one mailbox and one
credential, and it verifies at startup that the credential really opens that mailbox.

That choice is the whole point:

- **Your MCP client can govern mailboxes individually.** Tools are namespaced per
  server — `gmail-work:search_threads` and `gmail-personal:search_threads` are
  different tools, so permission rules, allow-lists and audit logs can distinguish
  them. With one multi-account server they are the same tool with a different
  argument, and no client-side rule can tell them apart.
- **A project can only reach what it registers.** A repo whose config names one
  mailbox cannot read another one, because no credential for it exists in that
  process.
- **A misfiled token fails loudly.** On startup the server calls the Gmail profile
  endpoint and compares the address against the one it was configured with. Mismatch
  means exit, not a server quietly serving the wrong inbox under the right name.

Composing and sending are two different tool calls, on purpose: `create_draft` only
ever writes to the Drafts folder. The one tool that can put a message in someone's
inbox, `send_draft`, takes a `draft_id` rather than a fresh `to`/`subject`/`body` — so
sending always means sending something the caller already has on screen. See
[Sending](#sending) below for the confirmation step this depends on. It still cannot
reply, forward, label, archive, trash or delete existing mail.

## Install

Requires Python 3.10+.

```bash
uvx --from git+https://github.com/YoCharge/gmail-mcp gmail-mcp
```

Or for development:

```bash
git clone https://github.com/YoCharge/gmail-mcp && cd gmail-mcp
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## Setup

One Google Cloud project and OAuth client covers every mailbox — full walkthrough in
[docs/setup.md](docs/setup.md). Then, once per mailbox:

```bash
export GMAIL_CLIENT_ID=...
export GMAIL_CLIENT_SECRET=...

gmail-mcp-auth you@example.com
```

A browser opens, you sign in as that mailbox, and a refresh token is printed. It is
verified against the address you asked for before it is shown, so signing in as the
wrong account fails immediately rather than months later.

## Configure

Add one entry per mailbox:

```jsonc
{
  "mcpServers": {
    "gmail-work": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/YoCharge/gmail-mcp", "gmail-mcp"],
      "env": {
        "GMAIL_ACCOUNT": "you@example.com",
        "GMAIL_CLIENT_ID": "...apps.googleusercontent.com",
        "GMAIL_CLIENT_SECRET": "...",
        "GMAIL_REFRESH_TOKEN": "1//0g...",
        "GMAIL_MCP_DOWNLOAD_DIR": "/absolute/path/for/attachments"
      }
    },
    "gmail-team": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/YoCharge/gmail-mcp", "gmail-mcp"],
      "env": {
        "GMAIL_ACCOUNT": "team@example.com",
        "GMAIL_CLIENT_ID": "...apps.googleusercontent.com",
        "GMAIL_CLIENT_SECRET": "...",
        "GMAIL_REFRESH_TOKEN": "1//0g..."
      }
    }
  }
}
```

This is committed as [`.mcp.json.example`](.mcp.json.example) — copy it, then replace
three values per mailbox.

Note what the two entries share and what they do not: the **client ID and secret are
the same for every mailbox**, because one OAuth client serves them all. Only
`GMAIL_ACCOUNT` and `GMAIL_REFRESH_TOKEN` differ — and it is that pairing the server
verifies at startup, so a token in the wrong entry stops the server rather than
silently opening the wrong inbox.

Refresh tokens are passwords. Keep your real config out of version control — the
`.gitignore` here already excludes `.mcp.json` while keeping the example.

## Tools

Eleven, each named with its mailbox in the description.

| Tool | Does |
|---|---|
| `search_threads(query, max_results=20)` | Full Gmail search syntax — `is:unread`, `from:`, `newer_than:7d` |
| `get_thread(thread_id)` | Every message in a thread: headers, decoded body, attachment ids |
| `get_message(message_id)` | One message |
| `list_labels()` | Label names and ids |
| `download_attachment(message_id, attachment_id, dest_dir?, filename?)` | Writes one attachment to disk |
| `list_drafts(max_results=20)` | Draft ids and snippets |
| `get_draft(draft_id)` | One draft's composed message |
| `create_draft(to, subject?, body?, cc?, bcc?, thread_id?)` | Writes a new draft. Never sent automatically |
| `update_draft(draft_id, to, subject?, body?, cc?, bcc?, thread_id?)` | Replaces an existing draft's contents |
| `delete_draft(draft_id)` | Deletes a draft. Cannot be undone |
| `send_draft(draft_id)` | Sends an existing draft. **Irreversible** — see [Sending](#sending) |

`download_attachment` requires a destination: either `dest_dir` on the call, or
`GMAIL_MCP_DOWNLOAD_DIR` in the environment. It will not guess a directory. Filenames
supplied by the sender are reduced to their final path component, so an attachment
named `../../.ssh/authorized_keys` writes as `authorized_keys`.

## Sending

Composing and sending are deliberately two separate tools:

1. `create_draft` (or `update_draft`) writes to the Drafts folder and echoes back the
   exact `to`/`subject`/`body`/`cc`/`bcc` it just wrote, together with a `draft_id`.
2. `send_draft(draft_id)` is the only call in this server that can put a message in
   someone's inbox. It takes just the id — not the content — so there is no way to
   compose and send in a single tool call.

That split exists so an agent (or the human driving it) always has a concrete draft
to review between "write this email" and "send it". Nothing here enforces that a human
actually looks at step 1's output before step 2 runs — that enforcement is the calling
agent's and MCP client's job (e.g. requiring approval before `send_draft` specifically)
— but the tool boundary makes that enforcement possible to add, where a single
`send_message(to, subject, body)` tool would not.

## Untrusted content

Anyone can email a mailbox you sweep. Every tool result is therefore prefixed with an
explicit notice marking the content as third-party data rather than instructions.
That is a mitigation, not a guarantee — if you point an agent at an inbox, treat
everything it reads as hostile input and keep the agent's other tools scoped
accordingly.

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `GMAIL_ACCOUNT` | yes | The mailbox this process serves. No default |
| `GMAIL_CLIENT_ID` | yes | OAuth client id |
| `GMAIL_CLIENT_SECRET` | yes | OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | yes | From `gmail-mcp-auth`, for this mailbox |
| `GMAIL_MCP_DOWNLOAD_DIR` | no | Fallback directory for `download_attachment` |

## Licence

MIT.
