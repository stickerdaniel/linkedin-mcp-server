// Paste into the Docker login viewer: Ctrl+V (Cmd+V on a Mac) types the clipboard's text into the remote page.
//
// The Dockerfile adds one <script> tag for this module to noVNC's own vnc.html; noVNC itself is unchanged. noVNC's
// keyboard handler swallows Ctrl+V on its canvas, so no paste event would ever fire. This module sees the key first
// (capture phase) and keeps it from noVNC without preventing the browser's default, so the browser fires a `paste`
// event with the text; then it releases the modifiers noVNC already pressed on the remote side and types the text key
// by key, paced, the way noVNC's own on-screen keyboard input does. If no paste event comes, it asks
// navigator.clipboard.readText() instead. Each Ctrl+V is typed once, by whichever of the two has its text first.
// Text is kept only while waiting to type; it is never logged or persisted. It uses only noVNC's public RFB API
// (sendKey, viewOnly, the disconnect event) and the UI's connected/rfb fields.
import UI from "./app/ui.js";
import KeyTable from "./core/input/keysym.js";
import keysyms from "./core/input/keysymdef.js";

const MAX_CHARS = 1000;
const PACE_MS = 20;
const FALLBACK_MS = 300;
const LATE_PASTE_MS = 5000;
const SPECIAL = { "\n": KeyTable.XK_Return, "\t": KeyTable.XK_Tab };
// The modifiers a paste shortcut can hold, by KeyboardEvent.code, with the keysym noVNC sends for each.
const MODIFIERS = {
  ControlLeft: KeyTable.XK_Control_L, ControlRight: KeyTable.XK_Control_R,
  ShiftLeft: KeyTable.XK_Shift_L, ShiftRight: KeyTable.XK_Shift_R,
  MetaLeft: KeyTable.XK_Super_L, MetaRight: KeyTable.XK_Super_R,
};
// On Windows noVNC holds a ControlLeft press back for 100 ms, in case AltGr follows, and only then sends it. A release
// sent sooner would come before the press it releases; this waits that out, with room to spare.
const ALTGR_MS = 150;

let typing = null;  // the current connection's drain and its pending pastes
let attempt = null;  // the latest Ctrl+V: when it was pressed, its fallback timer, whether it was typed or replaced
const held = new Map();  // modifiers that reached noVNC and are still down, with when each went down

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function connected() {
  return UI.connected && !UI.rfb.viewOnly;
}

function editable(target) {
  return target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

async function releaseHeldKeys(rfb) {
  // Ctrl/Cmd (and Shift) go up on the remote side first, so the text is typed as plain keys. noVNC still counts them
  // as pressed, so the person's own key-up later sends a second release, which changes nothing.
  const wait = Math.max(0, ...Array.from(held.values(), at => at + ALTGR_MS - Date.now()));
  if (wait) await sleep(wait);
  for (const code of held.keys()) rfb.sendKey(MODIFIERS[code], code, false);
  held.clear();
}

async function type(text) {
  if (!connected() || typeof text !== "string") return;
  text = text.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
  const chars = Array.from(text).slice(0, MAX_CHARS);
  if (!chars.length) return;
  if (typing && !typing.cancelled && typing.rfb === UI.rfb) {
    typing.pending.push(chars);
    return;
  }
  // A new connection must never receive the previous one's queued text, even if it reconnects during a sleep.
  if (typing) typing.cancel();
  const current = { rfb: UI.rfb, pending: [chars], cancelled: false };
  current.cancel = () => {
    current.cancelled = true;
    current.pending.length = 0;
  };
  typing = current;
  current.rfb.addEventListener("disconnect", current.cancel);
  try {
    while (current.pending.length) {
      if (current.cancelled || !connected() || UI.rfb !== current.rfb) return;
      await releaseHeldKeys(current.rfb);
      for (const ch of current.pending.shift()) {
        if (current.cancelled || !connected() || UI.rfb !== current.rfb) return;
        const cp = ch.codePointAt(0);
        const keysym = SPECIAL[ch] ?? (cp < 0x20 || cp === 0x7f ? 0 : keysyms.lookup(cp));  // other controls: skipped
        if (keysym) current.rfb.sendKey(keysym, null);
        await sleep(PACE_MS);
      }
    }
  } finally {
    current.cancel();
    current.rfb.removeEventListener("disconnect", current.cancel);
    if (typing === current) typing = null;
  }
}

// While the text is being typed, no key reaches noVNC: a held Ctrl repeating its key-down would press Ctrl on the
// remote side again and turn the rest of the text into shortcuts. (Its key-up is ignored by noVNC: already released.)
for (const kind of ["keydown", "keyup"]) {
  document.addEventListener(kind, event => {
    if (typing && !editable(event.target)) {
      event.stopImmediatePropagation();
      event.preventDefault();
      return;
    }
    if (!(event.code in MODIFIERS) || editable(event.target)) return;
    if (kind === "keyup") held.delete(event.code);
    else if (!held.has(event.code)) held.set(event.code, Date.now());
  }, true);
}
// noVNC releases every key when the page loses focus.
window.addEventListener("blur", () => held.clear());

document.addEventListener("keydown", event => {
  if (typing) return;
  const paste = (event.ctrlKey || event.metaKey) && !event.altKey && event.code === "KeyV";
  if (!paste || editable(event.target) || !connected()) return;
  // Not preventDefault: the browser's paste must still happen. Only noVNC must not see this key.
  event.stopPropagation();
  // Each Ctrl+V is an attempt of its own, so what an earlier one did never holds this one back. An earlier one whose
  // fallback is still waiting or still reading gives way to it.
  if (attempt) attempt.cancel();
  const current = { at: Date.now(), timer: null, typed: false, replaced: false, rfb: UI.rfb };
  current.cancel = () => {
    clearTimeout(current.timer);
    current.replaced = true;
    current.rfb.removeEventListener("disconnect", current.cancel);
  };
  attempt = current;
  current.rfb.addEventListener("disconnect", current.cancel);
  current.timer = setTimeout(() => {
    if (current.replaced || !connected() || current.rfb !== UI.rfb) return;
    if (!navigator.clipboard || !navigator.clipboard.readText) return;
    navigator.clipboard.readText().then(text => {
      // Its paste event came first, the attempt's connection ended, a later shortcut replaced it, or it is empty.
      if (current.typed || current.replaced || current.rfb !== UI.rfb || !text) return;
      current.typed = true;
      current.rfb.removeEventListener("disconnect", current.cancel);
      type(text);
    }, () => {});  // refused: nothing typed, so this Ctrl+V's paste event may still type its text
  }, FALLBACK_MS);
}, true);

document.addEventListener("paste", event => {
  if (editable(event.target) || !connected()) return;
  event.preventDefault();
  // The paste event of the latest Ctrl+V settles that attempt. One with no Ctrl+V before it, such as the browser's
  // own Paste menu, is typed as it is.
  const current = attempt && Date.now() - attempt.at < LATE_PASTE_MS ? attempt : null;
  if (current) {
    clearTimeout(current.timer);
    current.rfb.removeEventListener("disconnect", current.cancel);
    attempt = null;
    if (current.typed || current.replaced || current.rfb !== UI.rfb) return;
    current.typed = true;
  }
  type(event.clipboardData ? event.clipboardData.getData("text/plain") : "");
}, true);
