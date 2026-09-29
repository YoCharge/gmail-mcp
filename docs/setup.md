# Setup

One Google Cloud project and one OAuth client serve every mailbox you want to read.
You do this part once; after that each new mailbox is a single command.

## 1. Create a Google Cloud project

At [console.cloud.google.com](https://console.cloud.google.com), create a project (or
reuse one). Note its name — nothing else about it matters here.

## 2. Enable the Gmail API

**APIs & Services → Library → Gmail API → Enable.**

Without this the token exchange succeeds and every API call then fails with a
confusing `403 accessNotConfigured`.

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen.**

- **Google Workspace users: choose "Internal".** This is the important choice. Internal
  apps need no Google verification and their refresh tokens do not expire on a timer.
- **Personal Gmail users** can only choose "External". An External app left in
  **Testing** issues refresh tokens that **stop working after seven days** — the single
  most common cause of "it worked last week". Publish the app to move past that.

Add the scope `https://www.googleapis.com/auth/gmail.readonly`.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID.**

Choose **Desktop app**. The authorization flow runs a temporary listener on
`127.0.0.1` with a random port, which Desktop clients permit without registering
individual redirect URIs.

Copy the client ID and client secret.

## 5. Authorize each mailbox

```bash
export GMAIL_CLIENT_ID='...apps.googleusercontent.com'
export GMAIL_CLIENT_SECRET='...'

gmail-mcp-auth you@example.com
```

A browser opens. Sign in **as that mailbox** and approve. The command prints a refresh
token.

Before printing it, the token is checked against the address you asked for — so if you
were already signed in as a different account and approved with that one, it fails and
discards the token instead of handing you a credential for the wrong inbox.

Repeat per mailbox. Each produces its own refresh token.

## 6. Register the servers

One entry per mailbox, each with its own `GMAIL_ACCOUNT` and `GMAIL_REFRESH_TOKEN`.
See the README for a complete example.

Keep the config file out of version control: a refresh token opens that mailbox until
it is revoked.

## Revoking

At [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
signed in as the mailbox in question. Revoking invalidates that mailbox's refresh
token immediately and affects no other mailbox.

## Troubleshooting

**`Token refresh failed: invalid_grant`**
The refresh token is dead. Either it was revoked, the account's password changed, or
the consent screen is an External app still in Testing (seven-day expiry — see step 3).
Re-run `gmail-mcp-auth`.

**`Refresh token mismatch: GMAIL_ACCOUNT is 'a@example.com' but this token opens 'b@example.com'`**
A token is in the wrong server entry. This is the check doing its job — swap them, or
re-run `gmail-mcp-auth` for the right mailbox.

**`Google returned no refresh_token`**
That mailbox has already granted access to this client, so Google issued only an
access token. Revoke it at the link above and run the command again.

**`403 accessNotConfigured`**
The Gmail API is not enabled on the project (step 2).
