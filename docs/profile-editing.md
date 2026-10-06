# Editing your own LinkedIn profile through MCP

Ask an AI assistant to review your LinkedIn profile, see an exact diff of what
it suggests, approve it, and have the change made and checked for you, without
handing it a password or letting it change anything on its own.

```
You:    Read my LinkedIn profile and audit it from a recruiter-search
        perspective. I'm targeting senior product/software engineering
        with React, TypeScript, Node and AI.
Agent:  I'd change 7 things. Here is the diff… (nothing has changed yet)
You:    Apply those.
Agent:  Applied. Every field was re-read from LinkedIn and matches.
```

This guide covers the profile-editing tools added to
[linkedin-mcp-server](../README.md). Everything else about the server
(installation, login, Docker, proxies) is in the main README.

> [!WARNING]
> LinkedIn's User Agreement does not permit automated access, even to your own
> account. These tools are deliberately low-volume and human-directed, and they
> never try to evade LinkedIn's security checks, but use them knowing LinkedIn
> could restrict an account it considers automated.

---

## What it can do

| Field | Read | Edit |
|---|---|---|
| Headline | ✓ | ✓ |
| About | ✓ | ✓ |
| Experience: title | ✓ | ✓ |
| Experience: description | ✓ | ✓ |
| Skills | ✓ (all of them, with order) | add, remove |
| Name, location | ✓ | — |
| Company, dates, employment type, education, featured | partly | — returns `UNSUPPORTED_FIELD` |
| Creating or deleting positions | — | — never |
| Reordering skills | — | — |

The server carries text you have written or approved. It never invents jobs,
employers, dates, qualifications, skills or achievements, and the tool
descriptions tell the AI so.

## The safety model

Every change goes through the same five steps. Only the fourth writes to
LinkedIn.

```
READ ──► PROPOSE ──► PREVIEW ──► APPLY (you approve) ──► VERIFY
         nothing      nothing     writes, one field       re-reads each
         written      written     at a time               field from LinkedIn
```

1. **Two independent switches.** The server must be started with
   `MCP_LINKEDIN_WRITE_ENABLED=true`, *and* each apply call must pass
   `confirm: true`. Writes are off by default.
2. **A change set is applied at most once.** It moves
   `PENDING_APPROVAL → APPLYING → APPLIED / PARTIAL_FAILURE / FAILED`, or to
   `DISCARDED` / `STALE`, and never backwards.
3. **Your own edits are never overwritten.** A proposal records a fingerprint of
   every value it will change. Preview and apply read them again, and the editor
   checks the visible value once more before typing. If anything changed, you
   get `STALE_CHANGE_SET` and nothing is written.
4. **Bound to one account.** A change set records the profile it was planned
   for. If a different LinkedIn account is signed in later, preview and apply
   return `ACCOUNT_MISMATCH` and write nothing.
5. **No guessing.** Experiences are addressed by LinkedIn's own position id.
   A description such as "the IPG role" that fits more than one entry returns
   `AMBIGUOUS_EXPERIENCE` with the candidates. A `startDate` is matched against
   the start of each position's date range only.
6. **No truncation.** Text over a limit is refused with its length, the limit
   and the overflow. Limits are read from LinkedIn's form where it states them
   (the description field says "maximum 2,000 characters").
7. **Exact skills only.** A skill is added only when LinkedIn's own suggestion
   list contains that exact name; its spelling is kept (`reactjs` → `React.js`).
   Skills already on the profile are recognised and not added twice. If a
   skills list keeps loading past the reader's limit, the result is
   `INCOMPLETE_READ`, never a partial list.
8. **No broadcasts.** If a form's "notify your network" switch is on, for a
   text edit or a skill, the save is refused rather than announcing your edit.
9. **Success means observed.** A field is reported `verified: true` only after
   the saved value is read back from LinkedIn. Clicking Save is not success.
   A rich-text field that reads empty is trusted only after it stays empty for
   several seconds, so a slow editor is never mistaken for an empty field.
10. **Stop on the first problem.** Earlier fields stay applied and are reported,
   later ones are `NOT_ATTEMPTED`. Nothing is retried and nothing is rolled
   back automatically.
11. **Interruptions are recorded.** Progress is saved after every field. If an
   apply is cancelled or times out, the change set is closed with what is
   already live, and the field that was being written is marked
   `OUTCOME_UNKNOWN`: check it on linkedin.com. A record left mid-apply by a
   crashed process can be closed with `discard_profile_changes`, which returns
   its per-field results.
12. **Paced.** Writes are three seconds apart. There is no bulk mode.

## Setup

Install and sign in exactly as for the rest of the server (see
[Setup with uvx](../README.md#setup-with-uvx-recommended)). The profile tools
reuse the same browser session, so there is no separate login. Your password,
MFA codes and cookies never pass through MCP.

Reading and proposing work without any extra configuration. To allow the server
to apply approved changes, add the write flag.

**Claude Code**

```bash
claude mcp add linkedin --env MCP_LINKEDIN_WRITE_ENABLED=true -- uvx mcp-server-linkedin@latest
```

**Claude Desktop and other `mcpServers` clients**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["mcp-server-linkedin@latest", "--tool-timeout", "1200"],
      "env": { "MCP_LINKEDIN_WRITE_ENABLED": "true" }
    }
  }
}
```

`--tool-timeout 1200` gives a large change set time to finish: each field takes
several seconds to open, type, save and re-read.

| Setting | Default | Meaning |
|---|---|---|
| `MCP_LINKEDIN_WRITE_ENABLED` | off | Allow `apply_profile_changes` to write. |
| `LINKEDIN_PROFILE_EDITS_DIR` | `~/.linkedin-mcp/profile-edits` | Where change sets, snapshots and the audit log are kept. |

> To try it before it reaches a PyPI release, install from a git branch that
> carries it: `uvx --from git+https://github.com/<owner>/linkedin-mcp-server@<branch> mcp-server-linkedin`

## The tools

| Tool | Changes LinkedIn? | Purpose |
|---|---|---|
| `get_my_editable_profile` | No | Name, headline, location, About, experiences (with ids), skills, limits. |
| `get_my_experience` | No | All positions, or one position's exact title and description. |
| `get_my_skills` | No | Every skill, in LinkedIn's order. |
| `propose_profile_changes` | No | Validate requested values and store a change set with a diff. |
| `preview_profile_changes` | No | Show the diff again and confirm the profile hasn't changed since. |
| `apply_profile_changes` | **Yes** | Apply an approved change set and verify each field. |
| `discard_profile_changes` | No | Make a pending change set unusable. |

The existing `get_my_profile` (raw section text) is unchanged.

The three editing-read tools above deliberately return structured fields rather
than raw `sections` text. Their narrow exception to the section-reading return
format is defined in the [structured editing read contract](decisions/2026-10-06-structured-editing-reads.md).
Use the returned position ids and form values when proposing changes; do not
reconstruct them from rendered profile text.

### Proposing changes

```json
{
  "changes": {
    "headline": "Senior Product Engineer | React, TypeScript, Node.js | AI Products",
    "about": "First paragraph.\n\nSecond paragraph.",
    "experiences": [
      { "experienceId": "2449658942", "description": "What the role involved…" },
      { "match": { "company": "Liftango", "startDate": "2023" }, "title": "Product Developer" }
    ],
    "skills": { "add": ["TypeScript", "Node.js"], "remove": ["jQuery"] }
  }
}
```

Use the `experienceId` values from `get_my_editable_profile`. A `match` must fit
exactly one position. A blank line in text becomes a paragraph break on
LinkedIn.

The response contains the change set id, per-field before/after values,
warnings (for example "Skill 'React.js' is already on the profile"), and a
readable diff:

```
HEADLINE
before: Senior Software Developer
after:  Senior Product Engineer | React, TypeScript, Node.js | AI Products

SKILLS
+ TypeScript
+ Node.js
- jQuery
```

### Applying

```json
{ "changeSetId": "cs_c4a57ba7aa64b7fc", "confirm": true }
```

```json
{
  "changeSetId": "cs_c4a57ba7aa64b7fc",
  "status": "APPLIED",
  "results": [
    { "field": "headline", "status": "UPDATED", "verified": true },
    { "field": "skills/add/node.js", "status": "ADDED", "verified": true, "value": "Node.js" }
  ],
  "snapshotPath": "~/.linkedin-mcp/profile-edits/profile-history/2026-10-02T22-11-09_cs_….json"
}
```

## Results and errors

| Code | Meaning | What to do |
|---|---|---|
| `AUTHENTICATION_REQUIRED` | LinkedIn wants a sign-in or a security check. | Clear it on linkedin.com yourself, then retry. Nothing is bypassed. |
| `CONFIRMATION_REQUIRED` | Apply was called without `confirm: true`. | Approve the preview, then call again with `confirm: true`. |
| `WRITES_DISABLED` | The server was started without the write flag. | Restart it with `MCP_LINKEDIN_WRITE_ENABLED=true`. |
| `STALE_CHANGE_SET` | The profile changed after the proposal. | Read the profile again and propose afresh. |
| `CHANGE_SET_NOT_PENDING` | The change set was already applied, discarded or went stale. | Propose a new one. |
| `VALIDATION_ERROR` | Too long, an empty headline, a newline in a single-line field, or the notify switch is on. | Fix the value (`details` says which and by how much). |
| `AMBIGUOUS_EXPERIENCE` | A match fits several positions. | Pick one of the listed `experienceId`s. |
| `EXPERIENCE_NOT_FOUND` / `SKILL_NOT_FOUND` | No such position or skill; for skills, `offered` lists LinkedIn's suggestions. | Use an id or an offered name. |
| `UNSUPPORTED_FIELD` | A field this server doesn't edit, at any level of the request (`details.fields` names each one, e.g. `experiences[0].location`). | Edit it on linkedin.com. |
| `ACCOUNT_MISMATCH` | The change set was proposed for a different LinkedIn account than the one signed in now. | Sign back in to that account, or propose again from this one. |
| `INCOMPLETE_READ` | A list (such as skills) kept loading past the reader's limit, so it may be incomplete. | Retry; if it persists, the limit in `profile_editor.py` needs raising. |
| `SELECTOR_NOT_FOUND` | LinkedIn's page has changed. `details.dialog` lists the form's controls. | Update `profile_selectors.py` (see below). |
| `LINKEDIN_SAVE_FAILED` | LinkedIn refused the save; `formErrors` holds its message. | Read the message; propose again if needed. |
| `VERIFICATION_FAILED` | The re-read value differs from the approved one (`observed`). | Check the field on linkedin.com. |
| `PARTIAL_FAILURE` | Some fields applied and verified, then one failed. | Verified fields are live; propose a new change set for the rest. |

Per-field `status` values in apply results: `UPDATED`, `ADDED`, `REMOVED`
(all `verified: true`), `FAILED`, `NOT_ATTEMPTED`, and `OUTCOME_UNKNOWN` (the
apply was interrupted while this field was being written).

## Local records

Under `LINKEDIN_PROFILE_EDITS_DIR`:

- `change-sets/<id>.json`: every proposal, with before/after values, its
  fingerprint, status history and per-field results.
- `profile-history/<time>_<id>.json`: the values each apply replaced, written
  before anything changes. Use these to restore a field by hand or with a new
  change set.
- `audit.jsonl`: one line per tool event: time, tool, change set, field, result,
  verified.

None of these contain cookies, tokens, headers or browser storage; the audit
writer refuses any other key. Keep the directory out of version control
(`data/` and `profile-edits/` are already git-ignored in this repository).

## Trying it safely

1. Start without the write flag. Call `get_my_editable_profile` and compare it
   with linkedin.com.
2. Propose a one-word headline change, then preview it. Linkedin.com should be
   unchanged.
3. Apply without `confirm` (`CONFIRMATION_REQUIRED`), then with `confirm: true`
   (`WRITES_DISABLED`).
4. Restart with the write flag and apply. Expect `verified: true` and the new
   headline on linkedin.com.
5. Apply the same change set again: `CHANGE_SET_NOT_PENDING`.
6. Propose another change, edit the headline by hand on linkedin.com, then
   apply: `STALE_CHANGE_SET`, and your hand edit stays.
7. Restore your headline from `profile-history/` with one more change set.

Run the server with `--no-headless` to watch each form open, fill and save.

## How it works, and when LinkedIn changes

All LinkedIn-specific knowledge lives in one file,
[`linkedin_mcp_server/linkedin/profile_selectors.py`](../linkedin_mcp_server/linkedin/profile_selectors.py),
measured against LinkedIn's English UI on 2 October 2026:

- Each field is edited through LinkedIn's own edit form, opened by URL
  (`/in/<you>/edit/intro/`, `/edit/forms/summary/new/`,
  `/details/experience/edit/forms/<id>/`, `/skills/edit/forms/new/`), never by
  clicking around the profile.
- Forms are native `<dialog>` elements; the page also keeps hidden ad dialogs,
  so the editor uses the visible one that holds form controls.
- Headline, About and descriptions are rich-text editors that fill themselves
  after they appear; values are accepted only once two reads agree.
- Title and company are found through their accessible labels. Field ids are
  generated per render and never used.
- The skills page shows a bounded "All" view plus category views, each loading
  more as its own container scrolls. The reader visits every view and merges
  skills by LinkedIn's skill id.
- Visible text (button names such as "Save") is used only where nothing else
  identifies a control, and only through the per-locale `LABELS` table. On a
  non-English account the tools stop with `SELECTOR_NOT_FOUND` instead of
  clicking the wrong thing; add a locale to `LABELS` to support it.

The code is split so that only one module touches the browser:

| Module | Role |
|---|---|
| `profile_edit/changeset.py` | Validation, limits, diffs, fingerprints, status transitions. Browser-free. |
| `profile_edit/service.py` | The READ → PROPOSE → PREVIEW → APPLY → VERIFY workflow. Browser-free. |
| `profile_edit/store.py` | Change sets, snapshots and the audit log. |
| `linkedin/profile_editor.py` | Reads and writes LinkedIn's edit forms. |
| `linkedin/profile_selectors.py` | Every URL, locator and label it depends on. |
| `tools/profile_edit.py` | The MCP tools and their schemas. |

## Tests

```bash
uv run pytest tests/test_profile_edit_changeset.py tests/test_profile_edit_service.py \
              tests/test_profile_edit_tools.py tests/test_profile_editor_dom.py
```

- Unit tests for validation, limits, diffs, fingerprints, stale detection,
  experience matching, duplicate skills, state transitions and the audit log.
- Service tests against an in-memory profile, covering ambiguous experiences,
  missing About, no skills, over-long text, stale state, missing controls,
  LinkedIn save errors, partial success and security checkpoints.
- Browser tests in headless Chromium against synthetic pages that reproduce
  LinkedIn's measured form structure. No test contacts LinkedIn; the test
  browser blocks every request that would leave it.

CI never edits a real LinkedIn account. The flow above was run once by hand
against a real profile on 2 October 2026.
