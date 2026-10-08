"""Browser-UI message composition and send workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
import re
import time
from typing import Any, Literal
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse

import anyio
import anyio.lowlevel
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.destination import raise_if_off_linkedin
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
import linkedin_mcp_server.linkedin.contracts as contracts
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_person_identifier,
    normalize_profile_urn,
    person_profile_url,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

_MESSAGING_COMPOSE_SELECTOR = '[role="textbox"][contenteditable="true"]'

_PROFILE_MESSAGE_TARGET_JS = r"""() => {
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const active = anchor =>
        visible(anchor) &&
        !anchor.hasAttribute('disabled') &&
        (anchor.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const validComposeHref = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return false;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                url.pathname !== '/messaging/compose/'
            ) {
                return false;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            const normalized = values.map(item => {
                const text = item.trim();
                const prefix = 'urn:li:fsd_profile:';
                const identifier = text.startsWith(prefix)
                    ? text.slice(prefix.length)
                    : text;
                return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
            });
            return normalized.length > 0 &&
                normalized.every(item => item !== null && item === normalized[0]);
        } catch {
            return false;
        }
    };
    const main = document.querySelector('main');
    if (!main) return {status: 'unresolved'};

    // The top card is the first section that wraps no other section. LinkedIn
    // nests it inside a wrapper section and has moved the name between h1 and
    // h2, so neither its depth nor its heading level is pinned. It is chosen
    // before its heading is checked, so a card whose name has not rendered yet
    // stays unresolved instead of yielding to the next section. Sidebar
    // sections are skipped: they carry other people's Message links.
    const section = Array.from(main.querySelectorAll('section')).find(
        element =>
            visible(element) &&
            !element.closest('aside') &&
            !element.querySelector('section')
    );
    if (!section) return {status: 'unresolved'};
    const headings = Array.from(section.querySelectorAll('h1, h2, h3')).filter(visible);
    const visibleComposeAnchors = Array.from(
        section.querySelectorAll('a[href*="/messaging/compose/"]')
    ).filter(anchor => visible(anchor) && anchor.closest('section') === section);
    const composeAnchors = visibleComposeAnchors.filter(active);
    if (
        headings.length !== 1 ||
        composeAnchors.length > 1 ||
        (composeAnchors.length === 1 && visibleComposeAnchors.length !== 1)
    ) {
        return {status: 'unresolved'};
    }
    if (composeAnchors.length === 0) {
        return visibleComposeAnchors.length === 0
            ? {status: 'unavailable', pageUrl: window.location.href}
            : {status: 'unresolved'};
    }

    const anchor = composeAnchors[0];
    const composeHref = anchor.getAttribute('href') || anchor.href || '';
    if (!validComposeHref(composeHref)) return {status: 'unresolved'};
    return {
        status: 'resolved',
        pageUrl: window.location.href,
        displayName: normalize(
            headings[0].innerText || headings[0].textContent || ''
        ),
        composeHrefs: [composeHref],
    };
}"""

_PROFILE_MESSAGE_TARGET_READY_JS = (
    f"() => ({_PROFILE_MESSAGE_TARGET_JS})().status === 'resolved'"
)
_PROFILE_MESSAGE_TARGET_TIMEOUT_MS = 1_000
# The redesigned profile page (2026) renders its top card a few seconds after
# navigation, so a send, which asks right after `main` appears, waits up to
# ten. A profile read keeps the short wait: it asks only after the section text
# is in, and the wait ends early only on a resolved action, so every profile
# without one would hold the browser for the full ten seconds.
_SEND_PROFILE_MESSAGE_TARGET_TIMEOUT_MS = 10_000
_MESSAGE_SUBMIT_READY_TIMEOUT_MS = 1_000
_MESSAGE_CLEANUP_TIMEOUT_SECONDS = 1.0
# A send that may already have left has to answer (#889). FastMCP runs a tool
# inside `anyio.fail_after()`, and a deadline landing after the click discards
# whatever the send would have returned, so the work after dispatch stops this
# far ahead of the deadline the call runs under and answers `send_unconfirmed`
# itself. A sixth of the time left, the share AUTH_REPAIR_LOGIN_WAIT_FRACTION
# keeps for its reply, capped so a long TOOL_TIMEOUT does not cut confirmation
# short for nothing.
_SEND_REPLY_RESERVE_FRACTION = 1 / 6
_SEND_REPLY_RESERVE_SECONDS = 5.0

# Narrow exception to the generic-selector rule for #1107: enterToSend uses
# the send-toggle class only when the verified composer has no Send button.
# If the class changes, confirmed sends remain unavailable.
_MESSAGE_COMPOSER_INSPECT_JS = r"""
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const normalizeUrn = value => {
        const text = (value || '').trim();
        const prefix = 'urn:li:fsd_profile:';
        const identifier = text.startsWith(prefix) ? text.slice(prefix.length) : text;
        return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
    };
    const profilePath = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return null;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash
            ) {
                return null;
            }
            const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
            return match ? `/in/${match[1]}/` : null;
        } catch {
            return null;
        }
    };
    const messageRoute = target => {
        try {
            const url = new URL(window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                !(
                    url.pathname === '/messaging/compose/' ||
                    /^\/messaging\/thread\/[A-Za-z0-9_=-]+\/$/.test(url.pathname)
                )
            ) {
                return null;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            return values.every(value => normalizeUrn(value) === target.profileUrn)
                ? url.href
                : null;
        } catch {
            return null;
        }
    };
    const inspect = target => {
        const editors = Array.from(
            document.querySelectorAll('[role="textbox"][contenteditable="true"]')
        ).filter(visible);
        if (editors.length !== 1) return {status: 'ambiguous_editor'};
        const editor = editors[0];
        const semanticAncestors = element => {
            const scopes = [];
            let ancestor = element.parentElement;
            while (ancestor) {
                if (ancestor.matches('form, dialog, [role="dialog"]')) {
                    scopes.push(ancestor);
                }
                ancestor = ancestor.parentElement;
            }
            return scopes;
        };
        const localScopes = semanticAncestors(editor);
        if (localScopes.length === 0) return {status: 'missing_owner'};

        const owner = localScopes.find(scope =>
            scope.matches('dialog, [role="dialog"]')
        ) || localScopes[0];
        const outsideDraftAndHistory = element =>
            element !== editor &&
            !editor.contains(element) &&
            !element.closest('[data-view-name="message-list-item"]');
        const identityElements = selector => Array.from(new Set(
            localScopes.flatMap(scope => [
                ...(scope.matches(selector) ? [scope] : []),
                ...scope.querySelectorAll(selector),
            ])
        ));
        const paths = identityElements('a[href*="/in/"]')
            .filter(element => visible(element) && outsideDraftAndHistory(element))
            .map(anchor => profilePath(anchor.getAttribute('href') || anchor.href || ''));
        const urns = identityElements(
            '[data-profile-urn], [data-recipient-urn]'
        ).filter(
            element => visible(element) && outsideDraftAndHistory(element)
        ).flatMap(element =>
            ['data-profile-urn', 'data-recipient-urn']
                .filter(name => element.hasAttribute(name))
                .map(name => normalizeUrn(element.getAttribute(name)))
        );
        if (
            paths.some(path => path !== target.profilePath) ||
            urns.some(urn => urn !== target.profileUrn)
        ) {
            return {status: 'recipient_mismatch'};
        }

        const submitButtons = scope => Array.from(
            scope.querySelectorAll(
                'button[type="submit"], button[data-control-name="send"]'
            )
        ).filter(button =>
            visible(button) &&
            !button.closest('[data-view-name="message-list-item"]')
        );
        const localScope = localScopes.find(scope => submitButtons(scope).length > 0)
            || localScopes[0];
        const buttons = submitButtons(localScope);
        // With LinkedIn's "Press Enter to Send" preference the composer
        // renders no Send button, only the send-options toggle.
        const enterToSend = buttons.length === 0 && localScopes.some(scope =>
            Array.from(scope.querySelectorAll('.msg-form__send-toggle')).some(visible)
        );
        return {
            status: 'valid',
            editor,
            ancestorChain: localScopes,
            localScope,
            owner,
            buttons,
            enterToSend,
            active: document.activeElement === editor,
            empty: !(editor.innerText || '').replace(/\s+/g, ' ').trim(),
            messageRoute: messageRoute(target),
        };
    };
    // The element whose subtree holds this conversation's messages. An
    // overlay dialog holds both the messages and the composer. On the full
    // messaging page the owner is the composer <form> and the messages are
    // in its sibling, so climb to the nearest ancestor that holds a message
    // item, one visible editor, and stays below <main>. Otherwise keep the
    // owner, which leaves the send unconfirmed rather than widening the scope.
    const threadScope = owner => {
        if (!owner || owner.matches('dialog, [role="dialog"]')) return owner;
        const lists = '[data-view-name="message-list-item"]';
        let ancestor = owner.parentElement;
        while (ancestor && !ancestor.matches('main, body')) {
            const editors = Array.from(ancestor.querySelectorAll(
                '[role="textbox"][contenteditable="true"]'
            )).filter(visible);
            if (editors.length !== 1) return owner;
            if (ancestor.querySelector(lists)) return ancestor;
            ancestor = ancestor.parentElement;
        }
        return owner;
    };
"""

# How a multi-line message sits in the editor. Inserting it whole with
# `insertText` into the empty editor (`<p><br></p>`) gives one <p> per line and
# `<p><br></p>` per empty line (measured live, September 2026). LinkedIn's
# composer sends one line per top-level <p>, joined by LF, and reads only the
# text inside each; a <br> within a line would be dropped and merge two lines.
# So anything outside that grammar is not a message this tool can vouch for
# and reads as no lines at all. Chromium stores some typed spaces as NBSP
# (measured offline before a paragraph end), which is why lines compare with
# NBSP folded to a space. A message without LF keeps the plain comparison it
# always had.
_MESSAGE_EDITOR_LINES_JS = r"""
    const editorLines = editor => {
        const paragraphs = Array.from(editor.childNodes);
        if (paragraphs.length === 0) return null;
        const lines = [];
        for (const paragraph of paragraphs) {
            if (
                paragraph.nodeType !== Node.ELEMENT_NODE ||
                paragraph.tagName !== 'P' ||
                paragraph.attributes.length !== 0
            ) {
                return null;
            }
            const children = Array.from(paragraph.childNodes);
            let line = '';
            for (const [index, child] of children.entries()) {
                if (child.nodeType === Node.TEXT_NODE) {
                    line += child.data;
                } else if (
                    child.nodeType !== Node.ELEMENT_NODE ||
                    child.tagName !== 'BR' ||
                    index !== children.length - 1
                ) {
                    return null;
                }
            }
            lines.push(line);
        }
        return lines;
    };
    const sameLines = (lines, expected) =>
        lines !== null &&
        lines.length === expected.length &&
        lines.every(
            (line, index) =>
                line.replace(/\u00a0/g, ' ') === expected[index].replace(/\u00a0/g, ' ')
        );
    const editorMatches = (editor, text) => text.includes('\n')
        ? sameLines(editorLines(editor), text.split('\n'))
        : (editor.innerText || editor.textContent || '') === text;
"""

# The body of a multi-line message bubble, found by structure before any text
# is compared. Measured offline in ten message items from four pages captured
# in September 2026 (two threads, the inbox, a compose page): each item holds
# exactly one <p>, whose descendants are only <br>, <span> and <a>, and whose
# parent and grandparent are <div>s with the same rendered text; the sender
# link, name and time sit outside that pair. An item that breaks any of this
# has no body here, and a multi-line send in it stays unconfirmed. Text a
# future layout places outside the pair is not seen.
_MESSAGE_BODY_JS = r"""
    const messageBody = (item, requireVisible = true) => {
        const paragraphs = item.querySelectorAll('p');
        if (paragraphs.length !== 1) return null;
        const body = paragraphs[0];
        const parent = body.parentElement;
        const grandparent = parent?.parentElement;
        if (
            (requireVisible && !visible(body)) ||
            Array.from(body.querySelectorAll('*')).some(
                element => !['BR', 'SPAN', 'A'].includes(element.tagName)
            ) ||
            parent?.tagName !== 'DIV' ||
            grandparent?.tagName !== 'DIV' ||
            grandparent === item ||
            !item.contains(grandparent) ||
            parent.innerText !== body.innerText ||
            grandparent.innerText !== body.innerText
        ) {
            return null;
        }
        return body;
    };
"""

_MESSAGE_COMPOSER_OWNER_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const target = arg.target;
        const state = inspect(target);
        if (
            state.status !== 'valid' ||
            state.messageRoute !== arg.expectedRoute ||
            !state.owner.isConnected ||
            !state.editor.isConnected ||
            !state.owner.contains(state.editor) ||
            state.buttons.length !== 1
        ) {
            return null;
        }
        const button = state.buttons[0];
        if (
            !button.isConnected ||
            !state.localScope.contains(button) ||
            (button.form !== null && !state.ancestorChain.includes(button.form))
        ) {
            return null;
        }
        state.owner.__linkedinMcpComposer = {
            editor: state.editor,
            ancestorChain: state.ancestorChain,
            button,
            localScope: state.localScope,
            profilePath: target.profilePath,
            profileUrn: target.profileUrn,
            route: arg.expectedRoute,
            ownedMessage: null,
        };
        return state.owner;
    }"""
)

_MESSAGE_CONFIRMATION_PREPARE_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + _MESSAGE_EDITOR_LINES_JS
    + _MESSAGE_BODY_JS
    + r"""
        const composer = inspect(arg);
        const pinned = arg.owner?.__linkedinMcpComposer;
        if (
            composer.status !== 'valid' ||
            composer.messageRoute !== pinned?.route ||
            !pinned ||
            composer.owner !== arg.owner ||
            composer.editor !== pinned.editor ||
            composer.ancestorChain.length !== pinned.ancestorChain.length ||
            composer.ancestorChain.some(
                (scope, index) => scope !== pinned.ancestorChain[index]
            ) ||
            composer.localScope !== pinned.localScope ||
            composer.buttons.length !== 1 ||
            composer.buttons[0] !== pinned.button ||
            pinned.button.disabled ||
            (pinned.button.getAttribute('aria-disabled') || '').toLowerCase()
                === 'true' ||
            !arg.owner.isConnected ||
            !pinned.editor.isConnected ||
            !arg.owner.contains(pinned.editor) ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.expected ||
            !editorMatches(pinned.editor, arg.expected)
        ) {
            return null;
        }
        // What the bubble of a multi-line message should read, predicted from
        // the editor LinkedIn sends from (NBSPs included), using only what its
        // public bundles show: the send trims the text; the renderer turns
        // each LF into <br> and moves the whitespace run at either edge of a
        // line into a span holding one space; the bubble's CSS collapses runs
        // of spaces. Every other character, NBSP among them, stays as it is.
        const renderLine = line => (
            /\S/.test(line)
                ? line.replace(/^\s+/, ' ').replace(/\s+$/, ' ')
                : line
        ).replace(/ +/g, ' ');
        const rendered = arg.expected.includes('\n')
            ? editorLines(pinned.editor).join('\n').trim().split('\n')
                .map(renderLine).join('\n')
            : null;

        const counter = (arg.owner.__linkedinMcpConfirmationCounter || 0) + 1;
        arg.owner.__linkedinMcpConfirmationCounter = counter;
        const token = String(counter);
        const marker = document.createElement('span');
        marker.hidden = true;
        marker.setAttribute('data-linkedin-mcp-confirmation', token);
        marker.setAttribute('data-linkedin-mcp-invalid', 'false');
        arg.owner.appendChild(marker);
        pinned.editor.setAttribute('data-linkedin-mcp-editor', token);
        const state = {
            owner: arg.owner,
            scope: threadScope(arg.owner),
            editor: pinned.editor,
            expected: arg.expected,
            rendered,
            baseline: new Set(),
            candidates: new Map(),
            invalid: false,
        };
        const exactUnit = (node, requireVisible) => {
            if (requireVisible && !visible(node)) return false;
            if (state.rendered !== null) {
                const body = messageBody(node, requireVisible);
                // A removed node is no longer rendered, so its line breaks
                // cannot be read back. One that carried a body counts as a
                // match, which can only invalidate the observation.
                if (!requireVisible) return body !== null;
                return body !== null && body.innerText === state.rendered;
            }
            const elements = [node, ...node.querySelectorAll('*')].filter(
                element => !requireVisible || visible(element)
            );
            const matches = elements.filter(
                element => (element.innerText || '') === state.expected
            );
            const smallest = matches.filter(
                element => !matches.some(
                    other => other !== element && element.contains(other)
                )
            );
            return smallest.length === 1;
        };
        const remember = node => {
            if (!(node instanceof Element)) return;
            const items = [
                ...(node.matches('[data-view-name="message-list-item"]')
                    ? [node]
                    : []),
                ...node.querySelectorAll('[data-view-name="message-list-item"]'),
            ];
            for (const item of items) {
                if (state.baseline.has(item)) continue;
                if (!state.candidates.has(item)) {
                    item.setAttribute('data-linkedin-mcp-candidate', token);
                    state.candidates.set(item, {
                        transitioned: false,
                        matched: false,
                    });
                }
            }
        };
        const refresh = () => {
            for (const [node, candidate] of state.candidates) {
                if (
                    node.isConnected &&
                    state.scope.contains(node) &&
                    exactUnit(node, true)
                ) {
                    candidate.matched = true;
                    node.setAttribute('data-linkedin-mcp-matched', token);
                }
                if (candidate.matched && !node.isConnected) {
                    state.invalid = true;
                    marker.setAttribute('data-linkedin-mcp-invalid', 'true');
                }
            }
            if (
                Array.from(state.candidates.values()).filter(
                    candidate => candidate.matched
                ).length > 1
            ) {
                state.invalid = true;
                marker.setAttribute('data-linkedin-mcp-invalid', 'true');
            }
        };
        state.observer = new MutationObserver(records => {
            for (const record of records) {
                if (record.type !== 'childList') continue;
                for (const node of record.addedNodes) remember(node);
                for (const removed of record.removedNodes) {
                    if (!(removed instanceof Element)) continue;
                    if (removed === state.editor || removed.contains(state.editor)) {
                        state.invalid = true;
                        marker.setAttribute('data-linkedin-mcp-invalid', 'true');
                    }
                    for (const [candidate] of state.candidates) {
                        if (
                            (removed === candidate || removed.contains(candidate)) &&
                            exactUnit(candidate, false)
                        ) {
                            state.invalid = true;
                            marker.setAttribute(
                                'data-linkedin-mcp-invalid', 'true'
                            );
                        }
                    }
                }
            }
            for (const record of records) {
                if (
                    record.type !== 'attributes' ||
                    !state.candidates.has(record.target)
                ) {
                    continue;
                }
                const before = (record.oldValue || '').trim();
                const after = (
                    record.target.getAttribute('data-event-urn') || ''
                ).trim();
                if (before && after && before !== after) {
                    state.candidates.get(record.target).transitioned = true;
                    record.target.setAttribute(
                        'data-linkedin-mcp-transitioned', token
                    );
                }
            }
            refresh();
        });
        state.baseline = new Set(
            document.querySelectorAll('[data-view-name="message-list-item"]')
        );
        // Kept as an attribute: the readiness check runs in another world,
        // where properties set on elements here are not visible.
        marker.setAttribute('data-linkedin-mcp-route', window.location.pathname);
        marker.setAttribute('data-linkedin-mcp-baseline', JSON.stringify(
            Array.from(state.baseline)
                .map(node => (node.getAttribute('data-event-urn') || '').trim())
                .filter(Boolean)
        ));
        if (rendered !== null) {
            marker.setAttribute('data-linkedin-mcp-rendered', rendered);
        }
        state.observer.observe(state.scope, {
            attributes: true,
            attributeFilter: ['data-event-urn'],
            attributeOldValue: true,
            childList: true,
            subtree: true,
        });
        if (!arg.owner.__linkedinMcpConfirmations) {
            arg.owner.__linkedinMcpConfirmations = new Map();
        }
        arg.owner.__linkedinMcpConfirmations.set(token, state);
        return token;
    }"""
)

_MESSAGE_CONFIRMATION_READY_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + _MESSAGE_BODY_JS
    + r"""
        // A multi-line message is compared with the bubble text predicted at
        // prepare time, kept on the marker. Without it nothing confirms.
        const multiLine = arg.expected.includes('\n');
        const rendered = multiLine
            ? Array.from(
                arg.owner?.querySelectorAll('[data-linkedin-mcp-confirmation]') || []
            ).find(
                node => node.getAttribute('data-linkedin-mcp-confirmation') === arg.token
            )?.getAttribute('data-linkedin-mcp-rendered') ?? null
            : null;
        const exactVisibleUnit = node => {
            if (!visible(node)) return false;
            if (multiLine) {
                const body = messageBody(node);
                return rendered !== null && body !== null && body.innerText === rendered;
            }
            const elements = [node, ...node.querySelectorAll('*')].filter(visible);
            const matches = elements.filter(
                element => (element.innerText || '') === arg.expected
            );
            return matches.filter(
                element => !matches.some(
                    other => other !== element && element.contains(other)
                )
            ).length === 1;
        };
        const itemSelector = '[data-view-name="message-list-item"]';
        const linksRecipient = anchor => {
            let path;
            try {
                path = new URL(
                    anchor.getAttribute('href') || '', window.location.href
                ).pathname;
            } catch {
                return false;
            }
            const identifier = /^\/in\/([^/]+)/.exec(path)?.[1];
            return !!identifier && (
                identifier === arg.profileUrn || `/in/${identifier}/` === arg.profilePath
            );
        };
        // LinkedIn heads a message with links to its sender's profile
        // (measured). A follow-up from the same sender is assumed to be
        // unheaded, so the sender of a node is the nearest item at or before
        // it that links a profile. A
        // message from the recipient can carry the same text without this
        // submission ever reaching LinkedIn, so refuse a node the recipient
        // sent, or one whose sender cannot be found. Only the link path
        // counts, never its text.
        const sentByRecipient = (scope, node) => {
            const all = Array.from(scope.querySelectorAll(itemSelector));
            const sender = all.slice(0, all.indexOf(node) + 1).reverse().find(
                item => item.querySelector('a[href*="/in/"]')
            );
            return !sender || Array.from(
                sender.querySelectorAll('a[href*="/in/"]')
            ).some(linksRecipient);
        };
        // LinkedIn acknowledges a send by rendering a node whose event ID is
        // a server message URN. In an open thread it inserts that node and
        // removes its client-side placeholder; the first message of a new
        // thread moves the route to /messaging/thread/<id>/ and remounts the
        // whole conversation pane, composer included. Neither keeps the
        // observed node or the pinned composer, so accept exactly one visible
        // exact-text node carrying a server URN that was absent before submit,
        // inside the conversation pane of the one composer the page now shows.
        // That node must be the newest message in the pane, so older history
        // loaded later cannot stand in for it. A send that started on a
        // thread route must stay on that thread; leaving it for compose is
        // a different conversation too. One that started on the compose
        // route and now sits on a thread route is the first message of a
        // new thread, whose pane holds that one message; a pane with other
        // messages there is some other conversation, and so is one whose
        // header does not link the recipient.
        // Both arms exclude the new-message composer from thread identity.
        const threadRoute = path =>
            /^\/messaging\/thread\/(?!new\/)[A-Za-z0-9_=-]+\/$/.test(path);
        const routeContinues = marker => {
            const startPath = marker?.getAttribute('data-linkedin-mcp-route') || '';
            return !threadRoute(startPath) || window.location.pathname === startPath;
        };
        // A compose start that now sits on a thread is the first message of a
        // new thread: one visible item, and the pane links the recipient
        // outside the messages. Both arms, so a candidate cannot confirm a
        // pane the server arm would call some other conversation.
        const admittedTransition = (marker, scope) => {
            const startPath = marker?.getAttribute('data-linkedin-mcp-route') || '';
            const path = window.location.pathname;
            if (!threadRoute(path) || threadRoute(startPath)) {
                return true;
            }
            const visibleItems = Array.from(
                scope.querySelectorAll(itemSelector)
            ).filter(visible);
            return visibleItems.length === 1 &&
                Array.from(scope.querySelectorAll('a[href*="/in/"]')).some(
                    anchor => !anchor.closest(itemSelector) && linksRecipient(anchor)
                );
        };
        const serverAcknowledged = () => {
            const marker = Array.from(
                arg.owner?.querySelectorAll('[data-linkedin-mcp-confirmation]') || []
            ).find(
                node => node.getAttribute('data-linkedin-mcp-confirmation') === arg.token
            );
            if (!marker?.hasAttribute('data-linkedin-mcp-baseline')) return false;
            let baselineUrns;
            try {
                baselineUrns = new Set(
                    JSON.parse(marker?.getAttribute('data-linkedin-mcp-baseline'))
                );
            } catch {
                return false;
            }
            const composer = inspect(arg);
            if (composer.status !== 'valid' || composer.messageRoute === null) {
                return false;
            }
            const scope = threadScope(composer.owner);
            const items = Array.from(
                scope.querySelectorAll(itemSelector)
            ).filter(visible);
            if (!routeContinues(marker)) return false;
            if (!admittedTransition(marker, scope)) return false;
            const acknowledged = items.filter(node => {
                const urn = (node.getAttribute('data-event-urn') || '').trim();
                return urn.startsWith('urn:li:msg_message:') &&
                    !baselineUrns.has(urn) &&
                    exactVisibleUnit(node);
            });
            return acknowledged.length === 1 &&
                acknowledged[0] === items[items.length - 1] &&
                !sentByRecipient(scope, acknowledged[0]);
        };
        const confirmed = (() => {
            if (!arg.owner?.isConnected) return serverAcknowledged();
            const markers = Array.from(
                arg.owner.querySelectorAll('[data-linkedin-mcp-confirmation]')
            ).filter(
                marker => marker.getAttribute('data-linkedin-mcp-confirmation') === arg.token
            );
            if (
                markers.length !== 1 ||
                markers[0].getAttribute('data-linkedin-mcp-invalid') !== 'false'
            ) {
                return serverAcknowledged();
            }
            if (!routeContinues(markers[0])) return serverAcknowledged();
            const composer = inspect(arg);
            if (
                composer.status !== 'valid' ||
                composer.messageRoute === null ||
                composer.owner !== arg.owner ||
                composer.buttons.length !== 1 ||
                composer.editor.getAttribute('data-linkedin-mcp-editor') !== arg.token
            ) {
                return serverAcknowledged();
            }
            const scope = threadScope(arg.owner);
            const candidates = Array.from(
                scope.querySelectorAll('[data-linkedin-mcp-candidate]')
            ).filter(node =>
                node.getAttribute('data-linkedin-mcp-candidate') === arg.token &&
                node.getAttribute('data-linkedin-mcp-matched') === arg.token &&
                node.getAttribute('data-linkedin-mcp-transitioned') === arg.token &&
                (node.getAttribute('data-event-urn') || '').trim() &&
                exactVisibleUnit(node)
            );
            if (candidates.length === 1 && admittedTransition(markers[0], scope)) {
                return true;
            }
            return serverAcknowledged();
        })();
        // Resolves on any truthy value, so a miss stays null. The path is
        // the one this invocation just accepted.
        return confirmed ? {path: window.location.pathname} : null;
    }"""
)

_MESSAGE_CONFIRMATION_DISPOSE_JS = r"""arg => {
    const confirmations = arg.owner?.__linkedinMcpConfirmations;
    const state = confirmations?.get(arg.token);
    if (state?.observer) state.observer.disconnect();
    confirmations?.delete(arg.token);
    for (const element of (state?.scope || arg.owner)?.querySelectorAll(
        '[data-linkedin-mcp-candidate], [data-linkedin-mcp-editor], '
        + '[data-linkedin-mcp-confirmation]'
    ) || []) {
        for (const attribute of [
            'data-linkedin-mcp-candidate',
            'data-linkedin-mcp-matched',
            'data-linkedin-mcp-transitioned',
            'data-linkedin-mcp-editor',
        ]) {
            if (element.getAttribute(attribute) === arg.token) {
                element.removeAttribute(attribute);
            }
        }
        if (element.getAttribute('data-linkedin-mcp-confirmation') === arg.token) {
            element.remove();
        }
    }
}"""

_MESSAGE_COMPOSER_DISPOSE_JS = r"""owner => {
    const confirmations = owner?.__linkedinMcpConfirmations;
    const scopes = new Set([owner]);
    for (const state of confirmations?.values() || []) {
        if (state?.observer) state.observer.disconnect();
        if (state?.scope) scopes.add(state.scope);
    }
    confirmations?.clear();
    if (owner) {
        delete owner.__linkedinMcpConfirmations;
        delete owner.__linkedinMcpComposer;
    }
    const marked = Array.from(scopes).flatMap(scope => Array.from(
        scope?.querySelectorAll(
            '[data-linkedin-mcp-candidate], [data-linkedin-mcp-editor], '
            + '[data-linkedin-mcp-confirmation]'
        ) || []
    ));
    for (const element of new Set(marked)) {
        element.removeAttribute('data-linkedin-mcp-candidate');
        element.removeAttribute('data-linkedin-mcp-matched');
        element.removeAttribute('data-linkedin-mcp-transitioned');
        element.removeAttribute('data-linkedin-mcp-editor');
        if (element.hasAttribute('data-linkedin-mcp-confirmation')) element.remove();
    }
}"""

_MESSAGE_COMPOSER_STATE_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const state = inspect(target);
        return {
            status: state.status,
            active: state.active === true,
            empty: state.empty === true,
            submitCount: state.buttons ? state.buttons.length : 0,
            enterToSend: state.enterToSend === true,
            submitUsable: state.buttons?.length === 1 &&
                !state.buttons[0].disabled &&
                (state.buttons[0].getAttribute('aria-disabled') || '').toLowerCase()
                    !== 'true',
        };
    }"""
)

_MESSAGE_COMPOSER_READY_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        return inspect(target).status === 'valid';
    }"""
)

_MESSAGE_COMPOSER_FOCUS_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const state = inspect(target);
        if (state.status !== 'valid') return false;
        state.editor.focus();
        return state.editor.isConnected && document.activeElement === state.editor;
    }"""
)

_MESSAGE_COMPOSER_PINNED_JS = r"""
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const normalizeUrn = value => {
        const text = (value || '').trim();
        const prefix = 'urn:li:fsd_profile:';
        const identifier = text.startsWith(prefix) ? text.slice(prefix.length) : text;
        return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
    };
    const profilePath = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return null;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash
            ) {
                return null;
            }
            const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
            return match ? `/in/${match[1]}/` : null;
        } catch {
            return null;
        }
    };
    const messageRoute = target => {
        try {
            const url = new URL(window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                !(
                    url.pathname === '/messaging/compose/' ||
                    /^\/messaging\/thread\/[A-Za-z0-9_=-]+\/$/.test(url.pathname)
                )
            ) {
                return null;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            return values.every(value => normalizeUrn(value) === target.profileUrn)
                ? url.href
                : null;
        } catch {
            return null;
        }
    };
    const semanticAncestors = element => {
        const scopes = [];
        let ancestor = element?.parentElement;
        while (ancestor) {
            if (ancestor.matches('form, dialog, [role="dialog"]')) {
                scopes.push(ancestor);
            }
            ancestor = ancestor.parentElement;
        }
        return scopes;
    };
    const identitiesMatch = (scopes, editor, target) => {
        const outsideDraftAndHistory = element =>
            element !== editor &&
            !editor.contains(element) &&
            !element.closest('[data-view-name="message-list-item"]');
        const identityElements = selector => Array.from(new Set(
            scopes.flatMap(scope => [
                ...(scope.matches(selector) ? [scope] : []),
                ...scope.querySelectorAll(selector),
            ])
        ));
        const paths = identityElements('a[href*="/in/"]')
            .filter(element => visible(element) && outsideDraftAndHistory(element))
            .map(anchor => profilePath(anchor.getAttribute('href') || anchor.href || ''));
        const urns = identityElements(
            '[data-profile-urn], [data-recipient-urn]'
        ).filter(
            element => visible(element) && outsideDraftAndHistory(element)
        ).flatMap(element =>
            ['data-profile-urn', 'data-recipient-urn']
                .filter(name => element.hasAttribute(name))
                .map(name => normalizeUrn(element.getAttribute(name)))
        );
        return !(
            paths.some(path => path !== target.profilePath) ||
            urns.some(urn => urn !== target.profileUrn)
        );
    };
    const validatePinned = (target, requireEnabled = true) => {
        const pinned = owner?.__linkedinMcpComposer;
        if (
            !pinned ||
            pinned.profilePath !== target.profilePath ||
            pinned.profileUrn !== target.profileUrn ||
            messageRoute(target) !== pinned.route
        ) {
            return null;
        }
        const {editor, ancestorChain, button, localScope} = pinned;
        const currentChain = semanticAncestors(editor);
        if (
            !owner.isConnected ||
            !editor?.isConnected ||
            !button?.isConnected ||
            !localScope?.isConnected ||
            !Array.isArray(ancestorChain) ||
            currentChain.length !== ancestorChain.length ||
            currentChain.some((scope, index) => scope !== ancestorChain[index]) ||
            !currentChain.includes(owner) ||
            !currentChain.includes(localScope) ||
            !owner.contains(editor) ||
            !owner.contains(localScope) ||
            !localScope.contains(button) ||
            (button.form !== null && !currentChain.includes(button.form)) ||
            !visible(editor) ||
            !visible(button) ||
            !editor.matches('[role="textbox"][contenteditable="true"]') ||
            !identitiesMatch(currentChain, editor, target)
        ) {
            return null;
        }
        const buttons = Array.from(localScope.querySelectorAll(
            'button[type="submit"], button[data-control-name="send"]'
        )).filter(candidate =>
            visible(candidate) &&
            !candidate.closest('[data-view-name="message-list-item"]')
        );
        if (
            buttons.length !== 1 ||
            buttons[0] !== button ||
            (requireEnabled && (
                button.disabled ||
                (button.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ))
        ) {
            return null;
        }
        return pinned;
    };
"""

_MESSAGE_COMPOSER_WRITE_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + _MESSAGE_EDITOR_LINES_JS
    + r"""
        let pinned = validatePinned(arg, false);
        if (!pinned) return 'invalid';
        const {editor} = pinned;
        if ((editor.innerText || '').replace(/\s+/g, ' ').trim()) {
            return 'occupied';
        }
        editor.focus();
        pinned = validatePinned(arg, false);
        if (!pinned || document.activeElement !== editor) return 'invalid';
        if ((editor.innerText || '').replace(/\s+/g, ' ').trim()) {
            return 'occupied';
        }
        if (
            typeof document.queryCommandSupported !== 'function' ||
            !document.queryCommandSupported('insertText') ||
            typeof document.execCommand !== 'function'
        ) {
            return 'unsupported';
        }
        const inserted = document.execCommand('insertText', false, arg.message);
        if (editorMatches(editor, arg.message)) {
            pinned.ownedMessage = arg.message;
        }
        if (inserted !== true) return 'unsupported';
        pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== editor ||
            pinned.ownedMessage !== arg.message ||
            !editorMatches(editor, arg.message)
        ) {
            return 'invalid';
        }
        return 'written';
    }"""
)

_MESSAGE_COMPOSER_SUBMIT_READY_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + _MESSAGE_EDITOR_LINES_JS
    + r"""
        const pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            !editorMatches(pinned.editor, arg.message)
        ) {
            return 'invalid';
        }
        return pinned.button.disabled ||
            (pinned.button.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ? 'disabled'
            : 'ready';
    }"""
)

_MESSAGE_COMPOSER_CLEANUP_JS = (
    "(owner, arg) => {"
    + _MESSAGE_EDITOR_LINES_JS
    + r"""
    const pinned = owner?.__linkedinMcpComposer;
    if (!pinned || pinned.ownedMessage !== arg.message) return false;
    const {editor, ancestorChain} = pinned;
    const currentChain = [];
    let ancestor = editor?.parentElement;
    while (ancestor) {
        if (ancestor.matches('form, dialog, [role="dialog"]')) {
            currentChain.push(ancestor);
        }
        ancestor = ancestor.parentElement;
    }
    if (
        !owner.isConnected ||
        !editor?.isConnected ||
        !Array.isArray(ancestorChain) ||
        currentChain.length !== ancestorChain.length ||
        currentChain.some((scope, index) => scope !== ancestorChain[index]) ||
        !currentChain.includes(owner) ||
        !owner.contains(editor) ||
        !editorMatches(editor, arg.message)
    ) {
        return false;
    }
    pinned.ownedMessage = null;
    editor.replaceChildren();
    editor.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        composed: true,
        data: null,
        inputType: 'deleteContentBackward',
    }));
    return true;
}"""
)

_MESSAGE_COMPOSER_SUBMIT_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + _MESSAGE_EDITOR_LINES_JS
    + r"""
        const pinned = validatePinned(arg);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            !editorMatches(pinned.editor, arg.message)
        ) {
            return 'invalid';
        }
        pinned.button.click();
        return 'clicked';
    }"""
)

_LINKEDIN_MESSAGE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_PROFILE_PATH_RE = re.compile(r"^/in/[^/?#]+/$")
# A thread id is base64url and keeps its padding literally. Measured live:
# /messaging/thread/2-ZDBkMjZiY2Ut...XzEwMA==/ is what LinkedIn redirects an
# existing conversation to, and rejecting it stopped every send to a member
# the account had already written to. Only '=' is added: '%' would readmit an
# encoded slash and let one path pose as another. The id identifies nobody on
# its own, and the recipient is proven by the composer rather than this path.
_MESSAGE_THREAD_PATH_RE = re.compile(r"^/messaging/thread/[A-Za-z0-9_=-]+/$")
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"


@dataclass(frozen=True)
class _ProfileMessageTarget:
    profile_path: str
    profile_urn: str
    compose_url: str
    display_name: str | None


@dataclass(frozen=True)
class _ProfileMessageTargetResolution:
    status: Literal["resolved", "unavailable", "failed"]
    target: _ProfileMessageTarget | None = None


@dataclass(frozen=True)
class _SendConfirmation:
    """Thread segment of a confirmed send, or None on the compose route.

    Taken from the confirmation snapshot. A later page URL is not a source.
    """

    thread_id: str | None


def _confirmation_from_snapshot(snapshot: Any) -> _SendConfirmation | None:
    """Accept only ``{path}`` for a compose or thread route.

    ``wait_for_function`` resolves on every truthy value, so a snapshot that
    is not this shape is not a confirmation.
    """
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"path"}
        or not isinstance(snapshot["path"], str)
    ):
        return None
    path = snapshot["path"]
    if path in {"/messaging/compose/", "/messaging/thread/new/"}:
        return _SendConfirmation(thread_id=None)
    if _MESSAGE_THREAD_PATH_RE.fullmatch(path) is None:
        return None
    return _SendConfirmation(
        thread_id=path.removeprefix("/messaging/thread/").removesuffix("/")
    )


def _safe_linkedin_url(value: str, *, base: str | None = None) -> ParseResult | None:
    """Parse an HTTPS LinkedIn URL without credentials or an ambiguous origin."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    candidate = urljoin(base, value.strip()) if base else value.strip()
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower().removesuffix(".")
    if (
        parsed.scheme != "https"
        or not _LINKEDIN_MESSAGE_HOST_RE.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return None
    return parsed


def _normalize_profile_urn(value: str | None) -> str | None:
    """Return the identifier carried by a profile URN or raw recipient value."""
    if not isinstance(value, str):
        return None
    try:
        candidate = normalize_profile_urn(value)
    except LinkedInOperationError:
        return None
    return candidate.removeprefix(_PROFILE_URN_PREFIX)


# The 2026 profile page lands on /in/<name>/?isSelfProfile=false. That flag is
# the only query a profile route may carry; any other still refuses.
_BENIGN_PROFILE_QUERIES = {"", "isSelfProfile=false"}


def _profile_path_from_url(value: str) -> str | None:
    parsed = _safe_linkedin_url(value)
    if (
        parsed is None
        or parsed.query not in _BENIGN_PROFILE_QUERIES
        or not _PROFILE_PATH_RE.fullmatch(parsed.path)
    ):
        return None
    try:
        username = normalize_person_identifier(parsed._replace(query="").geturl())
    except LinkedInOperationError:
        return None
    canonical_path = urlparse(person_profile_url(username, "/")).path
    return parsed.path if parsed.path == canonical_path else None


def _profile_urn_from_compose_url(value: str, *, base: str | None = None) -> str | None:
    parsed = _safe_linkedin_url(value, base=base)
    if parsed is None or parsed.path != "/messaging/compose/":
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    identifiers: set[str] = set()
    for key in ("recipient", "profileUrn"):
        values = params.get(key, [])
        normalized = [_normalize_profile_urn(item) for item in values]
        if any(item is None for item in normalized):
            return None
        identifiers.update(item for item in normalized if item is not None)
    if len(identifiers) != 1:
        return None
    return identifiers.pop()


def _send_budget_deadline() -> float:
    """When the work after dispatch has to stop, on the event loop's clock."""
    deadline = anyio.current_effective_deadline()
    if math.isinf(deadline):
        return deadline
    remaining = max(0.0, deadline - anyio.current_time())
    return deadline - min(
        _SEND_REPLY_RESERVE_SECONDS, remaining * _SEND_REPLY_RESERVE_FRACTION
    )


def _enter_to_send_result(url: str) -> dict[str, Any]:
    """Report LinkedIn's "Press Enter to Send" preference as a user fix."""
    return contracts.message_action_result(
        url,
        "enter_to_send_enabled",
        "LinkedIn is set to 'Press Enter to Send', which hides the Send "
        "button this tool clicks. In LinkedIn Messaging, open the '...' menu "
        "next to 'Press Enter to Send', choose 'Click Send to send', then "
        "retry. Nothing was sent.",
        recipient_selected=True,
    )


def _message_page_url_is_safe(value: str, profile_urn: str) -> bool:
    parsed = _safe_linkedin_url(value)
    if parsed is None:
        return False

    params = parse_qs(parsed.query, keep_blank_values=True)
    recipient_values = [
        item for key in ("recipient", "profileUrn") for item in params.get(key, [])
    ]
    if parsed.path != "/messaging/compose/" and not _MESSAGE_THREAD_PATH_RE.fullmatch(
        parsed.path
    ):
        return False
    return all(_normalize_profile_urn(item) == profile_urn for item in recipient_values)


class MessageSender:
    """Compose and send messages through LinkedIn's browser UI."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._page = session.page

    async def _read_profile_message_target(
        self, *, timeout_ms: int = _PROFILE_MESSAGE_TARGET_TIMEOUT_MS
    ) -> _ProfileMessageTargetResolution:
        """Resolve one recipient-specific top-card compose action after settling."""
        try:
            await self._page.wait_for_function(
                _PROFILE_MESSAGE_TARGET_READY_JS,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the profile Message action", exc_info=True)

        try:
            # Not through `run_on_linkedin`: the script returns its document's
            # own `pageUrl`, and `_profile_path_from_url` below refuses any
            # address but a LinkedIn profile, so another site reads as failed.
            data = await self._page.evaluate(_PROFILE_MESSAGE_TARGET_JS)
        except Exception:
            logger.debug("Could not inspect the profile Message action", exc_info=True)
            return _ProfileMessageTargetResolution("failed")
        if not isinstance(data, dict):
            return _ProfileMessageTargetResolution("failed")
        # Said as the landing it is: read as a failed recipient, a portal that
        # replaced the profile would look like a profile without a Message
        # action, and nothing would name where the browser went.
        if isinstance(data.get("pageUrl"), str):
            raise_if_off_linkedin(data["pageUrl"])
        if data.get("status") == "unavailable":
            page_url = data.get("pageUrl")
            if (
                not isinstance(page_url, str)
                or _profile_path_from_url(page_url) is None
            ):
                return _ProfileMessageTargetResolution("failed")
            return _ProfileMessageTargetResolution("unavailable")
        if data.get("status") != "resolved":
            return _ProfileMessageTargetResolution("failed")

        page_url = data.get("pageUrl")
        compose_hrefs = data.get("composeHrefs")
        if not isinstance(page_url, str) or not isinstance(compose_hrefs, list):
            return _ProfileMessageTargetResolution("failed")
        profile_path = _profile_path_from_url(page_url)
        if profile_path is None:
            return _ProfileMessageTargetResolution("failed")
        if len(compose_hrefs) != 1 or not isinstance(compose_hrefs[0], str):
            return _ProfileMessageTargetResolution("failed")

        parsed_compose = _safe_linkedin_url(compose_hrefs[0], base=page_url)
        if parsed_compose is None:
            return _ProfileMessageTargetResolution("failed")
        compose_url = parsed_compose.geturl()
        profile_urn = _profile_urn_from_compose_url(compose_url)
        if profile_urn is None:
            return _ProfileMessageTargetResolution("failed")

        display_name = data.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        else:
            display_name = display_name.strip()
        return _ProfileMessageTargetResolution(
            "resolved",
            _ProfileMessageTarget(
                profile_path=profile_path,
                profile_urn=profile_urn,
                compose_url=compose_url,
                display_name=display_name,
            ),
        )

    async def _resolve_message_compose_href(self) -> str | None:
        """Return an unambiguous recipient-specific top-card compose URL."""
        resolution = await self._read_profile_message_target()
        return resolution.target.compose_url if resolution.target else None

    async def _wait_for_message_surface(
        self, target: _ProfileMessageTarget
    ) -> Literal["composer"] | None:
        """Wait for one editor with no contradictory local recipient identity."""
        if await self._wait_for_message_composer(target):
            return "composer"
        return None

    async def _wait_for_message_composer(self, target: _ProfileMessageTarget) -> bool:
        """Wait for the complete verified LinkedIn composer state to settle."""
        try:
            await self._page.wait_for_function(
                _MESSAGE_COMPOSER_READY_JS,
                arg=self._message_target_argument(target),
            )
        except PlaywrightTimeoutError:
            return False
        except Exception:
            logger.debug("Could not wait for the message editor", exc_info=True)
            return False
        return True

    async def _resolve_message_compose_box(self) -> Any | None:
        """Resolve the editor only when exactly one visible candidate exists."""
        locator = self._page.locator(f"{_MESSAGING_COMPOSE_SELECTOR}:visible")
        try:
            if await locator.count() != 1:
                return None
        except Exception:
            logger.debug("Could not count message editor candidates", exc_info=True)
            return None
        return locator.first

    @staticmethod
    def _message_target_argument(
        target: _ProfileMessageTarget,
    ) -> dict[str, str | bool]:
        return {
            "profilePath": target.profile_path,
            "profileUrn": target.profile_urn,
        }

    async def _read_message_composer_state(
        self, target: _ProfileMessageTarget
    ) -> dict[str, Any]:
        """Inspect the unique editor and reject contradictory local identity."""
        state = await self._page.evaluate(
            _MESSAGE_COMPOSER_STATE_JS,
            self._message_target_argument(target),
        )
        return state if isinstance(state, dict) else {"status": "invalid"}

    async def _focus_verified_message_editor(
        self, target: _ProfileMessageTarget
    ) -> bool:
        """Focus the same editor after local contradiction checks."""
        focused = await self._page.evaluate(
            _MESSAGE_COMPOSER_FOCUS_JS,
            self._message_target_argument(target),
        )
        return focused is True

    async def _write_verified_message(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str:
        """Insert text synchronously into the pinned local editor."""
        result = await owner.evaluate(
            _MESSAGE_COMPOSER_WRITE_JS,
            {**self._message_target_argument(target), "message": message},
        )
        return result if result in {"written", "occupied", "unsupported"} else "invalid"

    async def _wait_for_verified_submit(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> bool:
        """Wait briefly for the exact pinned submit button to become active."""
        deadline = time.monotonic() + _MESSAGE_SUBMIT_READY_TIMEOUT_MS / 1_000
        argument = {**self._message_target_argument(target), "message": message}
        while True:
            try:
                state = await owner.evaluate(
                    _MESSAGE_COMPOSER_SUBMIT_READY_JS, argument
                )
                if state == "ready":
                    return True
                if state != "disabled":
                    return False
            except Exception:
                logger.debug(
                    "Could not wait for the pinned submit button", exc_info=True
                )
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    async def _submit_verified_message(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str:
        """Click the one active submit button pinned with the local editor."""
        result = await owner.evaluate(
            _MESSAGE_COMPOSER_SUBMIT_JS,
            {**self._message_target_argument(target), "message": message},
        )
        return "clicked" if result == "clicked" else "invalid"

    @staticmethod
    async def _cleanup_owned_message(message: str, owner: Any) -> None:
        """Best-effort removal of text proven to belong to this tool call."""
        with contracts.before_the_reply_deadline(
            _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
        ) as scope:
            try:
                await owner.evaluate(_MESSAGE_COMPOSER_CLEANUP_JS, {"message": message})
            except Exception:
                logger.debug("Could not clear tool-owned message text", exc_info=True)
        if scope.cancel_called:
            logger.warning("Timed out clearing tool-owned message text")
        await anyio.lowlevel.checkpoint()

    async def _resolve_message_owner(
        self,
        target: _ProfileMessageTarget,
        *,
        expected_route: str,
    ) -> Any | None:
        """Hold the verified owner node across submission and confirmation."""
        owner = await self._page.evaluate_handle(
            _MESSAGE_COMPOSER_OWNER_JS,
            arg={
                "target": self._message_target_argument(target),
                "expectedRoute": expected_route,
            },
        )
        if owner.as_element() is None:
            await self._dispose_message_owner(owner)
            return None
        return owner

    @staticmethod
    async def _dispose_message_owner(owner: Any) -> None:
        """Release all owner-scoped observers, pins, markers and handles."""
        try:
            with contracts.before_the_reply_deadline(
                _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
            ) as dom_scope:
                try:
                    await owner.evaluate(_MESSAGE_COMPOSER_DISPOSE_JS)
                except Exception:
                    logger.debug("Could not clear pinned message nodes", exc_info=True)
            if dom_scope.cancel_called:
                logger.warning("Timed out clearing pinned message nodes")
        finally:
            with contracts.before_the_reply_deadline(
                _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
            ) as handle_scope:
                try:
                    await owner.dispose()
                except Exception:
                    logger.debug(
                        "Could not release message owner handle", exc_info=True
                    )
            if handle_scope.cancel_called:
                logger.warning("Timed out releasing message owner handle")
        await anyio.lowlevel.checkpoint()

    def _message_confirmation_argument(
        self,
        message: str,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> dict[str, Any]:
        return {
            **self._message_target_argument(target),
            "expected": message,
            "owner": owner,
        }

    async def _prepare_message_confirmation(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str | None:
        """Start the owner-scoped DOM observer immediately before submission."""
        token = await self._page.evaluate(
            _MESSAGE_CONFIRMATION_PREPARE_JS,
            self._message_confirmation_argument(message, target, owner),
        )
        return token if isinstance(token, str) and token else None

    async def _message_send_confirmed(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
        confirmation: str,
        reply_deadline: float = math.inf,
    ) -> _SendConfirmation | None:
        """Wait for LinkedIn to acknowledge the submitted message in its thread.

        Two signals count. The observer accepts a node inserted after it was
        installed whose exact visible message unit equals the typed text and
        which then changes from one non-empty ``data-event-urn`` value to a
        different one. Otherwise the conversation pane of the one composer on
        the page must hold exactly one visible exact-text node whose event ID
        is a server message URN absent before submission: LinkedIn replaces
        its client placeholder with that node, and the first message of a new
        thread remounts the pane under /messaging/thread/<id>/. Every timeout
        or ambiguity answers "not observed" because submission already
        happened.

        The predicate returns the path that invocation accepted. A thread
        path yields its segment; the compose path yields no thread. Failing
        to read or judge that snapshot is the same unconfirmed answer as a
        timeout. Disposal is not: a snapshot already accepted is kept when
        releasing its handle fails or runs out of time.
        """
        try:
            handle = await self._page.wait_for_function(
                _MESSAGE_CONFIRMATION_READY_JS,
                arg={
                    **self._message_target_argument(target),
                    "expected": message,
                    "owner": owner,
                    "token": confirmation,
                },
            )
        except Exception:
            logger.debug("Message send could not be confirmed", exc_info=True)
            return None

        confirmed: _SendConfirmation | None = None
        try:
            snapshot = await handle.json_value()
        except Exception:
            logger.debug(
                "Message confirmation snapshot could not be read", exc_info=True
            )
        else:
            confirmed = _confirmation_from_snapshot(snapshot)
            if confirmed is None:
                logger.debug("Message confirmation snapshot was not a route")
        finally:
            # Also when the snapshot is unusable. A caller with no deadline
            # still finishes: disposal carries its own cap.
            await self._dispose_confirmation_snapshot(handle, reply_deadline)
        return confirmed

    @staticmethod
    async def _dispose_confirmation_snapshot(
        handle: Any, reply_deadline: float
    ) -> None:
        """Release the handle that holds the confirmation path.

        Bounded by the deadline captured before the send budget and, when it
        is still live, by that budget too. The captured one matters once a
        stalled read has cancelled the budget, where the effective deadline
        reads as gone and a fresh bound would outlast the tool deadline. The
        live one matters when the snapshot arrived just before the budget
        ends: disposal has to finish inside it, or the budget cancels the
        accepted result on the way out.
        """
        now = anyio.current_time()
        limit = _MESSAGE_CLEANUP_TIMEOUT_SECONDS
        for deadline in (reply_deadline, anyio.current_effective_deadline()):
            if now < deadline < math.inf:
                limit = min(limit, (deadline - now) / 2)
        if limit <= 0:
            return
        with anyio.CancelScope(deadline=now + limit, shield=True) as scope:
            try:
                await handle.dispose()
            except Exception:
                logger.debug(
                    "Could not release message confirmation snapshot",
                    exc_info=True,
                )
        if scope.cancel_called:
            logger.warning("Timed out releasing message confirmation snapshot")
        await anyio.lowlevel.checkpoint()

    async def _dispose_message_confirmation(
        self, owner: Any, confirmation: str
    ) -> None:
        """Disconnect a request-local confirmation observer."""
        with contracts.before_the_reply_deadline(
            _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
        ) as scope:
            try:
                await self._page.evaluate(
                    _MESSAGE_CONFIRMATION_DISPOSE_JS,
                    {"owner": owner, "token": confirmation},
                )
            except Exception:
                logger.debug("Could not disconnect message observer", exc_info=True)
        if scope.cancel_called:
            logger.warning("Timed out disconnecting message observer")
        await anyio.lowlevel.checkpoint()

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """Compose and send a new message with explicit confirmation gating.

        Opens LinkedIn's profile-based compose flow. That may create a separate
        DM instead of replying in an existing recruiter/InMail or messaging
        thread. Recipient authorization comes from the validated top-card action
        carrying the target URN and the browser navigation it initiates. The exact
        resulting route is pinned through every later operation; visible local
        identities are optional corroboration, but any contradiction fails closed.

        Args:
            linkedin_username: LinkedIn username of the recipient.
            message: The message text to send.
            confirm_send: Must be True to actually send (False does a dry run).
            profile_urn: Optional profile URN (e.g. ACoAAB...) to verify against
                the recipient resolved from the loaded profile snapshot.
        """
        refusal = contracts.refuse_an_invalid_message(linkedin_username, message)
        if refusal is not None:
            return refusal
        # Every later step writes, compares and cleans up this one text, so
        # the editor holds what LinkedIn will send and the confirmation looks
        # for the same lines.
        message = contracts.normalize_message_text(message)
        linkedin_username = normalize_person_identifier(linkedin_username)
        if profile_urn is not None:
            profile_urn = normalize_profile_urn(profile_urn)
        profile_url = person_profile_url(linkedin_username, "/")
        # Read while the whole call is still ahead, so the reserve is a share
        # of the tool's time rather than of whatever navigation left over.
        budget_deadline = _send_budget_deadline()

        await self._navigator._navigate_to_page(profile_url)
        await self._session.check_rate_limit()

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        resolution = await self._read_profile_message_target(
            timeout_ms=_SEND_PROFILE_MESSAGE_TARGET_TIMEOUT_MS
        )
        if resolution.status == "unavailable":
            return contracts.message_action_result(
                profile_url,
                "message_unavailable",
                "LinkedIn did not expose a normal Message action for this profile. "
                "Use connect_with_person first, then retry only after the connection "
                "request is accepted.",
            )
        target = resolution.target
        if target is None:
            return contracts.message_action_result(
                profile_url,
                "recipient_resolution_failed",
                "LinkedIn did not expose one unambiguous recipient-specific Message "
                "action.",
            )

        supplied_urn = _normalize_profile_urn(profile_urn) if profile_urn else None
        if profile_urn is not None and supplied_urn != target.profile_urn:
            return contracts.message_action_result(
                profile_url,
                "recipient_resolution_failed",
                "The supplied profile URN did not match the loaded profile.",
            )

        # The validated top-card action and its browser navigation are the
        # recipient boundary. LinkedIn may strip the query and expose no local
        # identity, so capture the final route now and fail on any later change or
        # visible contradiction. Do not replace this with a Voyager/private API.
        # See docs/decisions/2026-09-16-rendered-page.md.
        await self._navigator._navigate_to_page(target.compose_url)
        expected_route = self._page.url
        if not _message_page_url_is_safe(expected_route, target.profile_urn):
            return contracts.message_action_result(
                expected_route,
                "recipient_resolution_failed",
                "LinkedIn opened an unexpected messaging URL.",
            )

        await self._session.check_rate_limit()
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Compose page did not fully load for %s", linkedin_username)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )

        message_surface = await self._wait_for_message_surface(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )
        logger.debug(
            "Message surface for %s was %s", linkedin_username, message_surface
        )
        if message_surface != "composer":
            return contracts.message_action_result(
                self._page.url,
                "composer_unavailable",
                "LinkedIn did not expose one usable message composer.",
            )

        state = await self._read_message_composer_state(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed during recipient verification.",
            )
        if state.get("status") != "valid":
            logger.debug(
                "Message recipient verification for %s returned %s",
                linkedin_username,
                state.get("status"),
            )
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The local composer did not identify exactly the requested profile.",
            )
        recipient_selected = True
        if state.get("enterToSend") is True:
            return _enter_to_send_result(self._page.url)

        if not confirm_send:
            return contracts.message_action_result(
                self._page.url,
                "confirmation_required",
                "Set confirm_send=true to send the message.",
                recipient_selected=recipient_selected,
            )

        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed before text entry.",
                recipient_selected=recipient_selected,
            )
        state = await self._read_message_composer_state(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed before text entry.",
                recipient_selected=recipient_selected,
            )
        if state.get("status") == "valid" and state.get("empty") is not True:
            # Text already in the editor belongs to whoever typed it. Clearing
            # it would trade a recipient leak for destroying their draft.
            return contracts.message_action_result(
                self._page.url,
                "composer_occupied",
                "The composer already holds a draft that would be sent along "
                "with the message. The draft was left untouched.",
                recipient_selected=recipient_selected,
            )
        if state.get("status") != "valid":
            return contracts.message_action_result(
                self._page.url,
                "compose_interact_failed",
                "The verified message composer changed before text entry.",
                recipient_selected=recipient_selected,
            )
        if state.get("enterToSend") is True:
            return _enter_to_send_result(self._page.url)
        if state.get("submitCount") != 1:
            return contracts.message_action_result(
                self._page.url,
                "send_unavailable",
                "The local submit path was missing or ambiguous.",
                recipient_selected=recipient_selected,
            )

        may_have_submitted = False
        try:
            owner = await self._resolve_message_owner(
                target, expected_route=expected_route
            )
            if owner is None:
                return contracts.message_action_result(
                    self._page.url,
                    "recipient_resolution_failed",
                    "The verified message composer changed before text entry.",
                    recipient_selected=recipient_selected,
                )

            try:
                write_result = await self._write_verified_message(
                    message,
                    target=target,
                    owner=owner,
                )
                if not _message_page_url_is_safe(self._page.url, target.profile_urn):
                    return contracts.message_action_result(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The messaging URL changed during text entry.",
                        recipient_selected=recipient_selected,
                    )
                if write_result == "occupied":
                    return contracts.message_action_result(
                        self._page.url,
                        "composer_occupied",
                        "The composer already holds a draft that would be sent along "
                        "with the message. The draft was left untouched.",
                        recipient_selected=recipient_selected,
                    )
                if write_result != "written":
                    return contracts.message_action_result(
                        self._page.url,
                        "compose_interact_failed",
                        "The verified message editor could not accept the message.",
                        recipient_selected=recipient_selected,
                    )

                if not await self._wait_for_verified_submit(
                    message,
                    target=target,
                    owner=owner,
                ):
                    return contracts.message_action_result(
                        self._page.url,
                        "send_unavailable",
                        "The pinned submit button did not become available without "
                        "changing the verified composer.",
                        recipient_selected=recipient_selected,
                    )

                confirmation = await self._prepare_message_confirmation(
                    message,
                    target=target,
                    owner=owner,
                )
                if confirmation is None:
                    return contracts.message_action_result(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The verified message composer changed before submission.",
                        recipient_selected=recipient_selected,
                    )

                if anyio.current_time() >= budget_deadline:
                    # Too little time is left to confirm a send, and nothing
                    # has been submitted. Wait for the deadline instead of
                    # clicking, so this ends like every other timeout before
                    # dispatch: an error the caller can safely retry on.
                    await anyio.sleep_forever()

                # The deadline the call runs under discards anything returned
                # after it, so the work after dispatch ends at this earlier
                # one and still answers. Only the tool's own deadline is
                # covered: a client that cancels gets no answer either way.
                # Captured before the budget: snapshot disposal runs after the
                # budget has cancelled, where this deadline would read as gone.
                reply_deadline = anyio.current_effective_deadline()
                budget = anyio.CancelScope(deadline=budget_deadline)
                try:
                    with budget:
                        try:
                            # A click can dispatch before the evaluate call
                            # reports an error, so an exception from this
                            # round trip is ambiguous.
                            may_have_submitted = True
                            submission = await self._submit_verified_message(
                                message,
                                target=target,
                                owner=owner,
                            )
                        except Exception:
                            logger.debug(
                                "Message submission did not complete", exc_info=True
                            )
                            return contracts.message_action_result(
                                self._page.url,
                                "send_unconfirmed",
                                "The message submission was interrupted and "
                                "LinkedIn did not confirm the send. Check the "
                                "conversation before retrying; retrying may "
                                "deliver the message twice.",
                                recipient_selected=recipient_selected,
                                retry_safe=False,
                            )

                        if submission != "clicked":
                            may_have_submitted = False
                            return contracts.message_action_result(
                                self._page.url,
                                "send_unavailable",
                                "The local submit path was missing, disabled, or "
                                "ambiguous.",
                                recipient_selected=recipient_selected,
                            )

                        confirmed = await self._message_send_confirmed(
                            message,
                            target=target,
                            owner=owner,
                            confirmation=confirmation,
                            reply_deadline=reply_deadline,
                        )
                        if confirmed is None:
                            return contracts.message_action_result(
                                self._page.url,
                                "send_unconfirmed",
                                "The message was submitted but LinkedIn did not "
                                "confirm the message-list transition in time. "
                                "Check the conversation before retrying; retrying "
                                "may deliver the message twice.",
                                recipient_selected=recipient_selected,
                                retry_safe=False,
                            )

                        return contracts.message_action_result(
                            self._page.url,
                            "sent",
                            "Message submitted and confirmed in the conversation UI.",
                            recipient_selected=recipient_selected,
                            sent=True,
                            retry_safe=False,
                            thread_id=confirmed.thread_id,
                        )

                    # Reached only when the budget ran out.
                    logger.debug("Message send reached its deadline unconfirmed")
                    return contracts.message_action_result(
                        self._page.url,
                        "send_unconfirmed",
                        "The tool deadline arrived before LinkedIn confirmed the "
                        "send, and the message may already have been submitted. "
                        "Check the conversation before retrying; retrying may "
                        "deliver the message twice.",
                        recipient_selected=recipient_selected,
                        retry_safe=False,
                    )
                finally:
                    await self._dispose_message_confirmation(owner, confirmation)
            finally:
                try:
                    if not may_have_submitted:
                        await self._cleanup_owned_message(message, owner)
                finally:
                    await self._dispose_message_owner(owner)
        except Exception:
            if not may_have_submitted:
                # Nothing can have been submitted yet, so the error itself is
                # the useful answer and the caller can retry on it.
                raise
            logger.debug(
                "Message send failed after a possible submission", exc_info=True
            )
            return contracts.message_action_result(
                self._page.url,
                "send_unconfirmed",
                "The message may already have been submitted when the send "
                "failed, and LinkedIn did not confirm the outcome. Check the "
                "conversation before retrying; retrying may deliver the "
                "message twice.",
                recipient_selected=recipient_selected,
                retry_safe=False,
            )
        except BaseException:
            # Cancellation the send's budget does not own: a client that
            # cancels or goes away. A cancelled scope discards whatever it
            # returns, so the answer the branch above gives cannot be given
            # here and the log line is all that is left.
            #
            # Silent before explicit submission: nothing can have left yet,
            # and a warning about duplicate delivery would be false.
            if may_have_submitted:
                logger.warning(contracts.SEND_INTERRUPTED_WARNING)
            raise
