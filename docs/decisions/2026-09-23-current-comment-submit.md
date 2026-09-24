# Current comment-submit identity

- Date: 2026-09-23
- Supersedes: `2026-09-18-evidence-of-a-write.md`

The evidence rule remains unchanged: text this server typed is not evidence of
a published comment, and only text LinkedIn renders outside every editable
region can confirm the write.

The submit-control identity has a second safe shape. Measured on the current
member-facing post detail page with workspace commit `1f72e79`:

- Before typing, the nearest compact composer row has exactly three buttons.
  All three have an `aria-label` and an SVG; exactly two have `aria-expanded`.
- Real key events append exactly one fourth button. It has `type="button"`, no
  `aria-label`, no `aria-expanded`, no `aria-pressed`, and no SVG.
- The earlier composer instead exposes exactly one enabled
  `type="submit"` button.
- Real key events add exactly one enabled, unlabeled, non-SVG `type="button"`
  to the pinned repost dialog. Its other controls are labelled or expanding.

Therefore a comment submit may be clicked only when it is either the one
enabled `type="submit"` control, the one control produced by that exact
three-to-four transition, or the structurally unique control in the pinned
repost dialog that was absent from the baseline recorded before typing. A
generic "only enabled button" fallback remains forbidden: before text, that
rule can select the photo attachment and publish nothing.

`PIN_EDITOR_JS` records the baseline, `SUBMIT_EDITOR_JS` enforces both accepted
shapes, and the browser-DOM suite holds the transition independently of label
values.
