# LinkedIn auth and restriction routes

The account-restriction route the server recognizes, and the observation
behind it. The matching code lives in `core/auth.py`, which also lists the
older not-signed-in routes (`/login`, `/checkpoint` and others) without a
recorded observation here. A new restriction route is added here first, with
its source and date.

Only the path is compared. The pages are localized, so their text says nothing
reliable about which one is showing.

## Account restriction

| Route | Seen | Source |
|---|---|---|
| `/flagship-web/login/login-restriction/` | 2026-09-27 | Live `--login` on a restricted account, in the Bengali UI ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147)) |

The page appeared right after the credentials were accepted, in place of the
feed. It said the account's access was temporarily restricted and asked for a
government-issued ID. It sets no `li_at` cookie, so a login that waits for one
never ends. The server matches the route by its final path segments,
`login/login-restriction`, which also covers it without the `flagship-web`
prefix. That prefixless form has not been seen.

A restricted account without a session is redirected to the ordinary `/login`
route first. The restriction shows only after signing in, so a tool call with no
session cannot detect it.

No other restriction route has been observed.
