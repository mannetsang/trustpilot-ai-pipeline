# Google Chat — read your spaces and DMs

Scripts that read Google Chat **as you** through the Chat API, so a Claude
session (or any script) can see every space, group chat and DM you are a
member of. Read-only scopes; nothing here can post.

## Acting as you, not as a Chat app

The Chat API has two identities, and only one of them sees "all my chats":

| Authenticated as | Sees |
|---|---|
| A **Chat app** (service account) | Only spaces the app was explicitly added to. Never DMs between people. |
| **You** | Everything you see: every space, group chat and DM, with full history. |

`chat_api.py` gets a "you" identity two ways, in this order:

1. **Domain-wide delegation (primary, set up).** The service account in
   `GOOGLE_APPLICATION_CREDENTIALS` impersonates `CHAT_IMPERSONATE_USER`
   (default `manne@superhairpieces.com`). This works in Claude cloud sessions
   with no token to store. The grant lives in admin.google.com → Security →
   API controls → Manage Domain Wide Delegation, on client ID
   `111336930970488121514` (`claude-sessions@shp-ai-bot-2026`), with:

   ```
   https://www.googleapis.com/auth/chat.spaces.readonly,https://www.googleapis.com/auth/chat.messages.readonly,https://www.googleapis.com/auth/chat.memberships.readonly
   ```

   Without `chat.memberships.readonly` the scripts still run, but DMs and
   group chats show as unnamed (they have no display name, only members).
2. **A user OAuth token (fallback)** from a one-time browser sign-in, for a
   machine with no delegated service account. Setup below.

## Browser sign-in (fallback only)

Console steps, all on project `shp-ai-bot-2026`:

1. **Chat app configuration** — APIs & Services → *Google Chat API* →
   *Configuration*: app name, avatar URL, description. Interactive features
   can stay off. Chat rejects user-auth calls from a project with no app
   configured.
2. **OAuth consent screen** — *Audience: Internal* (Workspace users only, so
   no Google verification).
3. **OAuth client** — APIs & Services → *Credentials* → *Create credentials*
   → *OAuth client ID* → type **Desktop app**. Download the JSON. It is a
   credential: keep it out of the repo and delete it after step 5.
4. On your machine, once:

   ```
   pip install -r google-chat/requirements.txt
   ```

5. Sign in and store the token (opens a browser; prints nothing sensitive):

   ```
   python google-chat/oauth_login.py --client-secrets %USERPROFILE%\Downloads\client_secret.json
   ```

   This stores the token as Secret Manager secret `google-chat-user-token`
   and grants `claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com`
   read access to that one secret. Add `--grant <other-sa>` for more, or
   `--no-grant` to store only.

## Use

```
python google-chat/list_spaces.py                              # every space/group chat/DM, most recent first
python google-chat/list_spaces.py --type DIRECT_MESSAGE
python google-chat/list_spaces.py --messages spaces/AAAAxxxx --limit 30
python google-chat/list_spaces.py --json
```

The token resolves through `lib/secrets.py`: `GOOGLE_CHAT_USER_TOKEN` in a
local `.env` first, then Secret Manager.

## Notes

- Group chats and DMs are listed only once at least one message has been
  sent in them. DMs have no display name; the table shows the other members
  instead (that's what the `chat.memberships.readonly` scope is for).
- Re-running `oauth_login.py` adds a new secret version. To widen the scopes,
  edit `SCOPES` in `chat_api.py` and sign in again.
- Revoke access any time at <https://myaccount.google.com/permissions>, then
  disable the secret version.
