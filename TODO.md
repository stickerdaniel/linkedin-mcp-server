# Fork Goals

## Summary

This fork adds LinkedIn **write capabilities** (posts, comments, articles) to the MCP server using the official LinkedIn REST API alongside the existing browser-scraping stack.

**API access is free for personal use.** The Community Management API Development tier costs nothing — 500 calls/app/day, 100 calls/member/day. The only gate is LinkedIn manually approving your developer app (self-serve application, no guaranteed timeline).

**One dedicated app required.** Consumer products (e.g. Share on LinkedIn) and Marketing Developer Platform products (Community Management API) cannot coexist on the same LinkedIn Developer app. Create a dedicated app for Community Management API only.

**Authentication** uses a one-time browser-assisted OAuth flow (`--linkedin-auth`) that opens the OS default browser, captures the callback locally, and stores access + refresh tokens at `~/.linkedin-mcp/api-tokens.json`. Users configure their LinkedIn Developer app credentials (`LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`) once; token refresh is automatic after that.

**Scope needed:** `w_member_social` covers all of Phase 1 (posts, comments) and Phase 2 (articles). Phase 3 (org posts) requires a separate business-verified approval and is low priority.

**Image uploads are deferred.** Phase 1 & 2 cover text and link posts only.

---

## Objective

Extend the MCP server with LinkedIn **content creation and editing** tools: personal posts, comments/replies, articles, and (lower priority) organization posts.

Approach: **Official LinkedIn API (Option B)**, hybrid with browser fallback if needed.

---

## Priority Order

1. Personal posts & comments (Phase 1)
2. Articles (Phase 2)
3. Organization posts (Phase 3)

---

## Target Tools

### Phase 1 — Personal Posts & Comments ✅
- [x] `create_post` — publish a text / text+link / image post as the authenticated member
- [x] `delete_post` — delete a post by URN or URL
- [x] `create_comment` — comment on a post
- [x] `reply_to_comment` — nested reply to an existing comment
- [x] `delete_comment` — delete a comment by URN

### Phase 2 — Articles
Note: uses `POST /rest/posts` with `w_member_social` scope — no Community Management API needed.
Endpoint requires explicit title, description, source URL, and thumbnail ImageUrn (uploaded separately).
- [ ] `create_article` — publish a long-form LinkedIn article (title, body, thumbnail)
- [ ] `edit_article` — update an existing article (commentary / lifecycle state)
- [ ] `delete_article` — delete a published article

### Phase 3 — Organization Posts (low priority)
- [ ] `create_org_post` — post on behalf of a company page (requires admin role)
- [ ] `create_org_comment` — comment on behalf of a company page

---

## LinkedIn API Cross-Reference

### Current codebase tools (all browser/Patchright — no API client exists)

| Tool | Module | Method |
|---|---|---|
| `get_person_profile` | tools/person.py | browser scrape |
| `search_people` | tools/person.py | browser scrape |
| `connect_with_person` | tools/person.py | browser DOM |
| `get_sidebar_profiles` | tools/person.py | browser scrape |
| `get_company_profile` | tools/company.py | browser scrape |
| `get_company_posts` | tools/company.py | browser scrape |
| `get_inbox` | tools/messaging.py | browser scrape |
| `get_conversation` | tools/messaging.py | browser scrape |
| `search_conversations` | tools/messaging.py | browser scrape |
| `send_message` | tools/messaging.py | browser DOM |
| `search_jobs` | tools/job.py | browser scrape |
| `get_job_details` | tools/job.py | browser scrape |
| `close_session` | server.py | browser lifecycle |

**No existing API client, no OAuth token handling, no `LINKEDIN_ACCESS_TOKEN` in config.**

---

### LinkedIn API Products — what we need

| Goal | API Product | Scope Required | Approval |
|---|---|---|---|
| Personal posts (create/delete) | Community Management API | `w_member_social` | Self-serve (Development tier) |
| Personal comments / replies | Community Management API | `w_member_social` | Self-serve (Development tier) |
| Articles | Community Management API | `w_member_social` | Self-serve (Development tier) |
| Read own posts/comments | Community Management API | `r_member_social` | **Restricted** — requires separate approval |
| Org posts | Page Management / Marketing Developer Platform | `w_organization_social` | **Requires business verification + use case review** |

**Bottom line:**
- Phase 1 & 2 need a single app with **Community Management API** (Development tier, self-serve). This grants `w_member_social`.
- **Important:** Consumer products (Share on LinkedIn, Sign In with LinkedIn) and Marketing Developer Platform products (Community Management API) **cannot be on the same app**. Use a dedicated app for the Community Management API only.
- `r_member_social` (reading your own posts back) is restricted; skip for now or rely on browser scraping for reads.
- Phase 3 needs formal business approval — deprioritized.

---

### API Endpoints (Posts API — new, replaces ugcPosts)

```
POST   /rest/posts                         # create post or article
GET    /rest/posts/{postUrn}               # get post
DELETE /rest/posts/{postUrn}               # delete post
POST   /rest/posts/{postUrn}?action=update # update commentary / lifecycle

POST   /rest/socialactions/{postUrn}/comments          # create comment
POST   /rest/socialactions/{commentUrn}/comments       # reply to comment
DELETE /rest/socialactions/{urn}/comments/{commentId}  # delete comment
```

Required headers on all calls:
```
LinkedIn-Version: YYYYMM   (e.g. 202506)
X-Restli-Protocol-Version: 2.0.0
Authorization: Bearer {access_token}
```

---

## What Was Built (Phase 1 complete ✅)

| Item | Status |
|---|---|
| `LinkedInApiConfig` in `config/schema.py` | ✅ |
| `--linkedin-auth` CLI command | ✅ |
| `api/app_credentials.py` — interactive app credential storage | ✅ |
| `api/tokens.py` — user OAuth token storage + refresh | ✅ |
| `api/auth.py` — OAuth flow with local callback server | ✅ |
| `api/client.py` — httpx client with auto-refresh | ✅ |
| `tools/posts.py` — 5 tools via `/v2/ugcPosts` + `/v2/socialActions` | ✅ |
| README LinkedIn API Setup section | ✅ |

**Actual endpoints used** (confirmed working against live LinkedIn):
- `POST /v2/ugcPosts` — create post
- `DELETE /v2/ugcPosts/{urn}` — delete post
- `POST /v2/socialActions/{urn}/comments` — create comment / reply
- `DELETE /v2/socialActions/{urn}/comments/{id}` — delete comment

**Required LinkedIn Developer app products** (both instant approval, consumer app):
- Share on LinkedIn → `w_member_social`
- Sign In with LinkedIn using OpenID Connect → `openid profile` (needed for person ID resolution)

---

## Integration Tests (TODO)

Write pytest integration tests for the API layer. These hit live LinkedIn so should be opt-in via an env var guard (e.g. `LINKEDIN_INTEGRATION_TESTS=1`).

- [ ] `test_create_and_delete_post` — create a post, assert 201 + URN, delete it, assert 200
- [ ] `test_create_and_delete_comment` — create post, comment on it, assert comment URN, delete comment, delete post
- [ ] `test_reply_to_comment` — create post → comment → reply, assert reply URN, clean up
- [ ] `test_auth_token_refresh` — mock expired token, assert refresh is triggered automatically
- [ ] `test_auth_flow` — mock the OAuth callback server, assert tokens are saved correctly

---

## Decisions

| Question | Decision |
|---|---|
| OAuth flow | Browser-assisted initial setup: open OS default browser (`webbrowser.open`) for LinkedIn OAuth consent, then store tokens locally under `~/.linkedin-mcp/` |
| Token storage | Access token + refresh token stored locally; server refreshes automatically on expiry |
| Token creation docs | Document LinkedIn Developer app setup + OAuth flow in README (how to get Client ID/Secret, which scopes to request) |
| Image uploads | **Deferred** — text/link posts only in Phase 1 & 2 |
