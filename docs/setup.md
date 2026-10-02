# Setup

Complete walkthrough, from nothing to a working server.

One Google Cloud project and one OAuth client serve **every** mailbox you want to
read. Steps 1–4 are done once. Step 5 is repeated per mailbox and takes about a
minute each.

## What you need first

- A Google account that can create a Cloud project. Any account can; no billing
  account and no payment method is required for this.
- The **password for each mailbox** you intend to read. You sign in as that mailbox
  during step 5. Being a Workspace administrator does **not** let you skip this — this
  server has no domain-wide-delegation path by design, so each mailbox grants access
  for itself.
- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) (or pip).

## The whole thing at a glance

| Step | Once, or per mailbox? | Produces |
|---|---|---|
| 1. Create a Cloud project | once | a project ID |
| 2. Enable the Gmail API | once | — |
| 3. Configure the consent screen | once | — |
| 4. Create an OAuth client | once | client ID + client secret |
| 5. Authorize a mailbox | **per mailbox** | a refresh token |
| 6. Register the server | **per mailbox** | a working MCP server |

If you prefer the command line, steps 1 and 2 are two commands — see
[the CLI fast path](#cli-fast-path-for-steps-1-and-2) below. Steps 3 and 4 have no
API and must be done in the console.

---

## 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the **project selector** in the top bar (it shows the current project name,
   next to "Google Cloud").
3. Click **New project** in the dialog that opens.
4. Give it a name — `gmail-mcp` is fine. The name is only shown to you.
5. Leave **Location** as "No organization" unless your Workspace requires otherwise.
6. Click **Create**, and wait for the notification that it is ready.
7. **Select the new project** from the project selector. Everything that follows must
   happen inside it.

Note the **project ID** shown on the project's dashboard. It looks like
`gmail-mcp-123456` — Google appends digits to make it globally unique, so it is
usually *not* the name you typed. You need the ID, not the name, for any CLI command.

An existing project works just as well; there is nothing special about a fresh one.

### CLI fast path (for steps 1 and 2)

```bash
gcloud projects create gmail-mcp-$RANDOM --name="gmail-mcp"   # prints the project ID
gcloud services enable gmail.googleapis.com --project=YOUR_PROJECT_ID
```

Steps 3 and 4 cannot be scripted — Google exposes no API for creating an OAuth
consent screen or client, so those two are console-only.

## 2. Enable the Gmail API

**APIs & Services → Library → search "Gmail API" → Enable.**

Or from the CLI:

```bash
gcloud services enable gmail.googleapis.com --project=YOUR_PROJECT_ID
```

**Do not skip this.** If the API is off, the OAuth flow still succeeds — you sign in,
you approve, a token is issued — and then the first API call fails. `gmail-mcp-auth`
catches this and tells you, but it is a confusing detour if you have not read this
step.

Confirm it took:

```bash
gcloud services list --enabled --project=YOUR_PROJECT_ID | grep gmail
# gmail.googleapis.com   Gmail API
```

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen.**

Pick the user type:

| Your situation | Choose | Consequence |
|---|---|---|
| Mailboxes are on a Google Workspace domain you administer | **Internal** | No Google verification needed. Refresh tokens do not expire on a timer. **Strongly preferred** |
| Personal Gmail accounts, or a domain you do not administer | **External** | Only option available. See the warning below |

> ⚠️ **External apps left in "Testing" issue refresh tokens that stop working after
> seven days.** This is the single most common cause of "it worked last week and now
> everything is broken". If you must use External, publish the app to move out of
> Testing, or accept re-authorizing weekly.

Add both scopes:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.compose`

Those are the only scopes this server uses. `gmail.compose` is what lets it create,
update and delete drafts — but no tool here calls the send endpoint, so there is no
code path that puts a message in anyone's inbox. It still cannot reply, forward,
label or delete existing mail.

## 4. Create the OAuth client and get the client ID and secret

**APIs & Services → Credentials → Create credentials → OAuth client ID.**

**Application type must be "Desktop app".** The authorization flow starts a temporary
listener on `127.0.0.1` with a random port; Desktop clients allow loopback redirects
without registering each port. Choosing "Web application" will fail with
`redirect_uri_mismatch`.

Name it anything — the name is only shown to you.

On clicking Create, a dialog shows the **Client ID** and **Client secret**. You can
copy them from there directly, or click **Download JSON** and read them from the file.
Both are always retrievable later from the same Credentials page, so nothing is lost
if you close the dialog.

### Reading them out of the downloaded file

The file lands in your Downloads folder named like
`client_secret_123456789012-abc123def456.apps.googleusercontent.com.json`, and looks
like this:

```json
{
  "installed": {
    "client_id": "123456789012-abc123def456.apps.googleusercontent.com",
    "project_id": "gmail-mcp-123456",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_secret": "GOCSPX-...",
    "redirect_uris": ["http://localhost"]
  }
}
```

The top-level key tells you whether you picked the right application type:

| Top-level key | Meaning |
|---|---|
| `"installed"` | Desktop app — **correct** |
| `"web"` | Web application — wrong type, create a new client |

Move it somewhere sensible and lock it down. A Downloads folder is not a credential
store:

```bash
mkdir -p ~/.config/gmail-mcp && chmod 700 ~/.config/gmail-mcp
mv ~/Downloads/client_secret_*.apps.googleusercontent.com.json ~/.config/gmail-mcp/client.json
chmod 600 ~/.config/gmail-mcp/client.json
```

Then export the two values without ever printing the secret to your terminal:

```bash
export GMAIL_CLIENT_ID=$(python3 -c "import json;print(json.load(open('$HOME/.config/gmail-mcp/client.json'))['installed']['client_id'])")
export GMAIL_CLIENT_SECRET=$(python3 -c "import json;print(json.load(open('$HOME/.config/gmail-mcp/client.json'))['installed']['client_secret'])")
```

Or with `jq`:

```bash
export GMAIL_CLIENT_ID=$(jq -r '.installed.client_id' ~/.config/gmail-mcp/client.json)
export GMAIL_CLIENT_SECRET=$(jq -r '.installed.client_secret' ~/.config/gmail-mcp/client.json)
```

Sanity check without revealing anything:

```bash
echo "client id set: ${GMAIL_CLIENT_ID:+yes} (${#GMAIL_CLIENT_ID} chars)"
echo "secret set:    ${GMAIL_CLIENT_SECRET:+yes} (${#GMAIL_CLIENT_SECRET} chars)"
```

The client ID and secret are the **same for every mailbox**. Only the refresh token
from step 5 differs.

## 5. Authorize each mailbox

With `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` exported:

```bash
gmail-mcp-auth you@example.com
```

A browser opens. **Sign in as that mailbox** and approve. If several Google accounts
are already signed in, pick carefully — this is the easiest mistake to make here.

Two checks run before anything is printed:

1. Google must return a refresh token.
2. The token's own profile must report the address you asked for.

Only then is the refresh token printed. If you approved with the wrong account, the
token is discarded and you are told so — you do not end up with a working credential
for the wrong inbox.

Repeat per mailbox. Each run produces its own refresh token.

## 6. Register the servers

One entry per mailbox, each with its own `GMAIL_ACCOUNT` and `GMAIL_REFRESH_TOKEN`,
sharing the same client ID and secret. See the README for a full example.

Keep that config file out of version control. A refresh token opens the mailbox until
it is revoked.

## 7. Confirm it works

Before wiring it into a client, check the credentials directly:

```bash
export GMAIL_ACCOUNT=you@example.com
export GMAIL_REFRESH_TOKEN='1//0g...'      # from step 5
# GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET already exported from step 4

gmail-mcp
```

A working setup prints one line to stderr and then waits for MCP traffic on stdin:

```
gmail-mcp: serving you@example.com (read + drafts, never sends)
```

That line means the refresh token was exchanged **and** the mailbox identity was
verified. Press Ctrl-C to stop it.

Anything else is a failure with a reason attached — see Troubleshooting below. The
server never starts in a half-working state: it either confirms the mailbox or exits
non-zero.

Finally, restart your MCP client after editing its config. Most clients read server
definitions once at startup, so a newly added server commonly shows as connected
while offering no tools until a restart.

---

## Revoking

At [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
signed in **as the mailbox in question**. Revoking kills that mailbox's refresh token
immediately and affects no other mailbox.

## Troubleshooting

**`Consent succeeded, but the mailbox could not be verified: Gmail API has not been used in project … or it is disabled`**
Step 2. Enable the Gmail API, wait a minute for it to propagate, run again.

**`Token refresh failed: invalid_grant`**
The refresh token is dead — revoked, the account's password changed, or it is an
External app still in Testing and seven days have passed (step 3). Re-run
`gmail-mcp-auth`.

**`Refresh token mismatch: GMAIL_ACCOUNT is 'a@example.com' but this token opens 'b@example.com'`**
A token is in the wrong server entry. This is the startup check doing its job. Swap
them, or re-run `gmail-mcp-auth` for the right mailbox.

**`Google returned no refresh_token`**
That mailbox already granted access to this client, so Google issued only an access
token. Revoke at the link above and run again.

**`redirect_uri_mismatch`**
The OAuth client is a "Web application" rather than a "Desktop app" (step 4). Create a
new Desktop client.

**`403 accessNotConfigured`**
Same as the first entry: the Gmail API is not enabled on the project.

**Draft tools fail with `insufficient authentication scopes` even though search/read tools work**
The refresh token was issued before `gmail.compose` was added to the consent screen
and this flow. Re-run `gmail-mcp-auth you@example.com` to get a new refresh token that
covers both scopes, and update that mailbox's `GMAIL_REFRESH_TOKEN`.
