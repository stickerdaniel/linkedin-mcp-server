# Easy Apply markup, as measured

What LinkedIn's Easy Apply dialog publishes, read off a live page rather than
assumed. Every line here carries the date and the posting it came from, and a
later reading that disagrees replaces it.

Measured 2026-09-23 against job posting `4412345678`, a posting whose
first step is "Contact info", signed in as an ordinary member. The dialog was
opened and its draft discarded; no step was advanced.

## The dialog lives in an open shadow root

The whole dialog renders inside the open shadow root of `div#interop-outlet`,
a host that is present on the posting page before the dialog exists. With the
dialog open, the document itself sees none of it:

| query on `document` | result |
| --- | --- |
| `[role="dialog"]` | 0 |
| `[role="combobox"]` | 0 |
| `[data-easy-apply-next-button]` | 0 |
| `[role="option"]` (list open) | 0 |

The root is open, so a CSS locator reaches through it and a hand-written
`document.querySelector` does not. That is the whole reason the dialog is
addressed with Playwright locators and the programs that do run in the page
walk `element.shadowRoot` themselves.

## The typeahead control

```html
<input id="single-typeahead-entity-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-4412345678-31234567890-location-GEO-LOCATION"
       class="basic-input " required="" dir="auto" role="combobox"
       aria-autocomplete="list" aria-activedescendant="" aria-expanded="false"
       type="text">
```

The id names the form element and the question (`...-location-GEO-LOCATION`),
and the wrapper `div` around the input repeats it with a `-ta` suffix. It is
LinkedIn's own handle, and nothing reads it apart from the step read, which
hands it back so an answer can reach the control it came from.

- `role="combobox"` is what separates it from a plain text input, and it is
  present before anything is typed. It is what the step read classifies on.
- `aria-activedescendant` is present and stays empty, with the list open and
  no arrow key pressed. Nothing in the DOM says which option a keystroke would
  take.
- The label beside it says its text twice, once `aria-hidden="true"` and once
  `class="visually-hidden"`. Read raw it doubles.

**No control carries the entity.** The question block holds no
`input[type="hidden"]`, and no other element carries the geo URN; the binding
between the visible string and the entity lives in component state only. There
is therefore no DOM reading of whether a pick took, beyond the field's value
and whether the list closed.

## The suggestion list

Typing opens a `div[role="listbox"]` with an id of the form
`triggered-expanded-ember<N>`, **inside the shadow root, as a sibling of the
input** within the field's wrapper `div` — not portalled to the document body.
While it is open the input carries both `aria-owns` and `aria-controls`, each
holding that same id; both are absent while it is closed.

Options are `div[role="option"][aria-selected="false"]` with ids of the form
`basic-result-<token>`. They are not list items. Each wraps its text in nested
spans across several lines, so the node's `textContent` is the string padded
with newlines and indentation; collapsing whitespace is required to match it.
The option itself carries no `aria-hidden` copy of its text, where the label
does.

Suggestions arrived within 300ms of the last keystroke.

## The vocabulary

`Paris` returned 20 suggestions, in this order at the top:

1. `Paris, Île-de-France, France`
2. `Paris, Texas, United States`
3. `Paris, Tennessee, United States`
4. `Orleans Parish County, Louisiana, United States`

The rest were further `Paris,` municipalities and Louisiana parishes. The
first is written the way LinkedIn writes a profile's location (city,
region, country), so a value read off a profile names a real suggestion
and would have been accepted. The ranking answers to the query alone and not to
where the member is: an account outside the United States asking for `Paris`
is offered American counties above `Paris, Ontario, Canada`, and only an
answer written in full reaches the one it means.

## What a prefill is worth

On `4412345678` the field was empty and required, although the same account
had sent an Easy Apply application four days earlier on a posting whose contact
step carried the same question and could not have been sent without answering
it. A past answer is therefore not reliably prefilled into a later form, and a
form that asks is asking for real. A second posting read the same day,
`4487654321`, had no location question on its contact step at all, so which
questions a step carries is the posting's own.

The account was not opted out of it: LinkedIn's own "Resumes and application
data" setting read **On** the same day, under the words "Allow LinkedIn to save
your resumes and answers to application questions". Saving being on is
therefore not the same as this answer coming back.

## Not measured

Whether advancing a step saves the answer to the account, or whether only
submitting does. Answering it costs a step advanced on a real posting.
