# Structured own-profile editing reads

- Date: 2026-10-06
- Supersedes: none

The raw section-text return contract applies to section-reading tools. The only
structured own-profile editing reads are `get_my_editable_profile`, `get_my_experience`
and `get_my_skills`, which support editing the signed-in member's own profile.
`get_my_profile` and every other section-reading tool retain their existing
`{url, sections: {name: raw_text}}` contract.

These editing reads expose form values and stable position ids for proposals,
baseline comparisons and verification. Rendered profile text can truncate values
and cannot reliably identify an edit target. Putting serialised fields in a
`sections` string would falsely present reconstructed data as raw page text.
The editor still reads LinkedIn's rendered pages and edit forms; this exception
does not permit private API requests.

Successful editing-read responses are:

- `get_my_editable_profile`: `url`, `name`, `headline`, `location`, `about`,
  `experiences`, `skills` and `limits`.
- `get_my_experience`: `experiences` when listing positions, or `experience`
  containing the requested position's exact title, description and `limits`.
- `get_my_skills`: `skills`, with each skill's `name` and `position`.

The existing structured error responses and approval gates are unchanged.
`tests/test_profile_edit_tools.py` checks the successful editing reads through
MCP, including preserved paragraphs, position ids and skill order.
