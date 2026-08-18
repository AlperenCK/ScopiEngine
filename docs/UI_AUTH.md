# Web UI authentication

The web UI (`/_ui/`, see [USAGE.md](USAGE.md#web-ui)) can require a login. This
is scoped deliberately narrowly:

- **Only `/_ui/` is gated.** The REST API (`/{index}/_search`, `/_bulk`, …) and
  the CLI are unauthenticated, exactly as they always were. This is not an
  oversight — ScopiEngine has no concept of a request-level identity or
  permissions anywhere else, and bolting a partial one onto the API surface
  would be a false sense of security, not a real one.
- **Off by default.** An installation that upgrades keeps its UI reachable
  exactly as before; nobody is locked out by a version bump.
- **One login method today: Service Accounts** — a username and password
  ScopiEngine itself stores and verifies. AD/LDAP bind login (verifying
  against an external directory instead) is planned as a second method, not a
  replacement.

## Turning it on

```bash
export SCOPI_UI_AUTH_ENABLED=true
```

or the equivalent `"ui_auth_enabled": true` in a JSON config file. There is no
CLI flag for it — it is a server setting, not a per-invocation one, the same
as `max_sort_candidates` or `buffer_mb`.

Enabling it with zero accounts locks the UI: the UI has no way to create its
own first account without already being logged into it. Bootstrap one first:

```bash
scopi ui-account create alice
# Password: ********
# Repeat for confirmation: ********
# created UI account 'alice'
```

From then on, `/_ui/` redirects an unauthenticated browser to
`/_ui/login.html`, and any `/_ui/api/*` call without a valid session gets a
`401`. Once logged in, more accounts can be created from the UI's Settings
tab — `scopi ui-account create` is only needed for the very first one.

## Managing accounts

```bash
scopi ui-account create <username>    # prompts for a password (min. 8 chars)
scopi ui-account list
scopi ui-account disable <username>
scopi ui-account enable <username>
scopi ui-account delete <username>
```

Every one of these also has a Settings-tab equivalent once you are logged in,
backed by the same `/_ui/api/accounts` endpoints — the CLI exists for the one
case the UI cannot cover itself: the first account.

**Disabling or deleting an account takes effect immediately**, not just once
its session's TTL naturally expires — every `/_ui/` request re-checks that
the session's account still exists and is still enabled, not only that the
session token itself is unexpired. A revoked operator is locked out on their
very next click, not up to `ui_session_ttl` seconds later.

## Sessions

A successful login sets an `httponly`, `SameSite=Lax` cookie
(`scopi_ui_session`) scoped to the `/_ui/` path — never sent to the REST API,
never readable by page JavaScript. The cookie carries a random 256-bit token;
only its SHA-256 hash is ever persisted, the same reasoning as a password
hash — reading the storage backend's contents does not hand out working
sessions.

| Setting | Default | Meaning |
|---|---|---|
| `SCOPI_UI_AUTH_ENABLED` | `false` | Require a login for `/_ui/`. |
| `SCOPI_UI_SESSION_TTL` | `43200` (12h) | Seconds a login stays valid before it must be re-established. |

Passwords are hashed with PBKDF2-HMAC-SHA256 (260,000 iterations, a random
16-byte salt per account) via the standard library only — no extra
dependency for something this common.

## What this does not cover

- **The REST API itself.** Anyone who can reach the server can still call
  `/{index}/_search`, `/_bulk`, and everything else, with or without
  `SCOPI_UI_AUTH_ENABLED`. If that needs to change, put ScopiEngine behind a
  reverse proxy that authenticates the whole surface, not just `/_ui/`.
- **Roles or permissions.** Every UI account can manage every other UI
  account — there is no "admin" vs. "read-only" distinction yet. Treat a
  Service Account as equivalent to shared operator access, not a
  per-person identity with scoped privileges.
- **AD/LDAP bind login.** Planned as a second login method alongside Service
  Accounts, not implemented yet.
