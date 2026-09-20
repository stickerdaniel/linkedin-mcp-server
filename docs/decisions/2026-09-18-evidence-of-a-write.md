# Evidence of a write

- Date: 2026-09-18
- Supersedes: none

A write is confirmed by something only LinkedIn could have produced. Text this
server put on the page is not that, and neither is a click it chose to make.

Measured on a live comment box on 2026-09-18, after `comment_on_post` reported
two comments published that did not exist:

- The comment editor is a descendant of the post container. Counting elements
  whose text equals the comment therefore counts the draft, and the count rises
  on typing alone — before any submit, and equally when the submit clicked the
  wrong control or LinkedIn refused the comment. The confirmation compared the
  draft against itself and passed with nothing published.
- Excluding the editor alone does not fix that. A composer whose other controls
  are icons contributes no text of its own, so the wrapping form's `innerText`
  *is* the draft and the form inherits the match. Every ancestor of an editor is
  excluded for this reason. A comment LinkedIn rendered is a sibling of the
  composer, never an ancestor of one.
- `document.execCommand('insertText')` fills the element and reads back
  verbatim, so every check available from inside the page passes, while
  LinkedIn's own editor state never updates and its submit control is never
  drawn. Only a real click followed by real key events produced a submittable
  comment. The message composer accepts `execCommand`; the comment box does not.
- Before a comment box has text there is no submit control at all. The buttons
  present are an emoji trigger, which carries `aria-expanded`, and a photo
  attachment, which carries nothing that distinguishes it. A rule that falls
  back to "the only enabled button left" selects the photo button, clicks it,
  opens a file picker, and publishes nothing.

So: a submit control is identified by `type="submit"` and never by elimination,
and a control that cannot be identified is a refusal. Refusing costs a retry.
Guessing clicks an unknown control on a page whose controls publish things.

`COUNT_TEXT_UNITS_JS` and `SUBMIT_EDITOR_JS` in `scraping/post_actions.py` carry
this rule. `tests/test_post_actions_dom.py` holds it against a real DOM in four
label sets, and the empty-aria set is the one that catches the ancestor case: in
a locale whose buttons are worded, the same markup hides it.
