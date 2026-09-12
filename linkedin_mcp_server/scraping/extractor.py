"""Core extraction engine using innerText instead of DOM selectors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import ParseResult, parse_qs, quote_plus, urljoin, urlparse

import anyio
import anyio.lowlevel
from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
)
from linkedin_mcp_server.scraping import contracts
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.company import CompanyScraper
from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    # Re-exported, not used: the search filters that raise it moved to
    # `search_urls`, while the MCP tool wrappers still catch the class through
    # this module. The redundant alias is what marks that as deliberate.
    FilterValidationError as FilterValidationError,
    # Re-exported for the same reason: the job workflow held the last call
    # inside this module, while `tools/company.py` and `tools/feed.py` still
    # build their rate-limit entry through this name.
    rate_limited_section_error as rate_limited_section_error,
)
from linkedin_mcp_server.scraping.feed import FeedScraper
from linkedin_mcp_server.scraping.identifiers import (
    messaging_thread_url,
    normalize_thread_id,
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.job_pages import JobPageReader
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.person import PersonScraper
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)
from linkedin_mcp_server.scraping.search_urls import build_content_search_url
from linkedin_mcp_server.scraping.text import (
    strip_conversation_chrome,
    strip_linkedin_noise,
)


if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5

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

    const section = Array.from(main.children).find(
        element => element.matches('section') && visible(element)
    );
    if (!section) return {status: 'unresolved'};
    const headings = Array.from(section.querySelectorAll('h1')).filter(
        heading => visible(heading) && heading.closest('section') === section
    );
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
_MESSAGE_SUBMIT_READY_TIMEOUT_MS = 1_000
_MESSAGE_CLEANUP_TIMEOUT_SECONDS = 1.0

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
        return {
            status: 'valid',
            editor,
            ancestorChain: localScopes,
            localScope,
            owner,
            buttons,
            active: document.activeElement === editor,
            empty: !(editor.innerText || '').replace(/\s+/g, ' ').trim(),
            messageRoute: messageRoute(target),
        };
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
            (pinned.editor.innerText || pinned.editor.textContent || '') !== arg.expected
        ) {
            return null;
        }

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
            editor: pinned.editor,
            expected: arg.expected,
            baseline: new Set(),
            candidates: new Map(),
            invalid: false,
        };
        const exactUnit = (node, requireVisible) => {
            if (requireVisible && !visible(node)) return false;
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
                    state.owner.contains(node) &&
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
        state.observer.observe(state.owner, {
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
    + r"""
        if (!arg.owner?.isConnected) return false;
        const markers = Array.from(
            arg.owner.querySelectorAll('[data-linkedin-mcp-confirmation]')
        ).filter(
            marker => marker.getAttribute('data-linkedin-mcp-confirmation') === arg.token
        );
        if (
            markers.length !== 1 ||
            markers[0].getAttribute('data-linkedin-mcp-invalid') !== 'false'
        ) {
            return false;
        }
        const composer = inspect(arg);
        if (
            composer.status !== 'valid' ||
            composer.messageRoute === null ||
            composer.owner !== arg.owner ||
            composer.buttons.length !== 1 ||
            composer.editor.getAttribute('data-linkedin-mcp-editor') !== arg.token
        ) {
            return false;
        }
        const exactVisibleUnit = node => {
            if (!visible(node)) return false;
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
        const candidates = Array.from(
            arg.owner.querySelectorAll('[data-linkedin-mcp-candidate]')
        ).filter(node =>
            node.getAttribute('data-linkedin-mcp-candidate') === arg.token &&
            node.getAttribute('data-linkedin-mcp-matched') === arg.token &&
            node.getAttribute('data-linkedin-mcp-transitioned') === arg.token &&
            (node.getAttribute('data-event-urn') || '').trim() &&
            exactVisibleUnit(node)
        );
        return candidates.length === 1;
    }"""
)

_MESSAGE_CONFIRMATION_DISPOSE_JS = r"""arg => {
    const confirmations = arg.owner?.__linkedinMcpConfirmations;
    const state = confirmations?.get(arg.token);
    if (state?.observer) state.observer.disconnect();
    confirmations?.delete(arg.token);
    for (const element of arg.owner?.querySelectorAll(
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
    for (const state of confirmations?.values() || []) {
        if (state?.observer) state.observer.disconnect();
    }
    confirmations?.clear();
    if (owner) {
        delete owner.__linkedinMcpConfirmations;
        delete owner.__linkedinMcpComposer;
    }
    for (const element of owner?.querySelectorAll(
        '[data-linkedin-mcp-candidate], [data-linkedin-mcp-editor], '
        + '[data-linkedin-mcp-confirmation]'
    ) || []) {
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
        if ((editor.innerText || editor.textContent || '') === arg.message) {
            pinned.ownedMessage = arg.message;
        }
        if (inserted !== true) return 'unsupported';
        pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== editor ||
            pinned.ownedMessage !== arg.message ||
            (editor.innerText || editor.textContent || '') !== arg.message
        ) {
            return 'invalid';
        }
        return 'written';
    }"""
)

_MESSAGE_COMPOSER_SUBMIT_READY_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        const pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            (pinned.editor.innerText || pinned.editor.textContent || '') !== arg.message
        ) {
            return 'invalid';
        }
        return pinned.button.disabled ||
            (pinned.button.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ? 'disabled'
            : 'ready';
    }"""
)

_MESSAGE_COMPOSER_CLEANUP_JS = r"""(owner, arg) => {
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
        (editor.innerText || editor.textContent || '') !== arg.message
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

_MESSAGE_COMPOSER_SUBMIT_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        const pinned = validatePinned(arg);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            (pinned.editor.innerText || pinned.editor.textContent || '') !== arg.message
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
_PROFILE_URN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
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
    candidate = value.strip()
    if candidate.startswith(_PROFILE_URN_PREFIX):
        candidate = candidate[len(_PROFILE_URN_PREFIX) :]
    return candidate if _PROFILE_URN_RE.fullmatch(candidate) else None


def _profile_path_from_url(value: str) -> str | None:
    parsed = _safe_linkedin_url(value)
    if parsed is None or parsed.query or not _PROFILE_PATH_RE.fullmatch(parsed.path):
        return None
    try:
        username = normalize_person_identifier(value)
    except LinkedInScraperException:
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


class LinkedInExtractor:
    """Extracts LinkedIn page content via navigate-scroll-innerText pattern."""

    def __init__(self, page: Page):
        self._session = ScrapingSession(page)
        self._navigator = PageNavigator(self._session)
        self._content = PageContentReader(self._session)
        self._capture = SectionCapture(self._session, self._navigator, self._content)
        self._feed = FeedScraper(self._session, self._navigator, self._content)
        # Late-bound on purpose: the top-card read the URN comes from still
        # lives here until the message sender owns it, and a bound method
        # captured now would not see a replacement installed on this instance.
        self._profile_page = ProfilePageReader(
            self._session, lambda: self._read_profile_message_target()
        )
        self._person = PersonScraper(
            self._session, self._navigator, self._capture, self._profile_page
        )
        self._company = CompanyScraper(self._session, self._capture)
        # Narrow and late-bound, like the reader above: the workflow needs one
        # main-profile read and nothing else of the person scraper, and
        # resolving `scrape_person` at call time keeps the facade's own frozen
        # delegate on that path rather than capturing what it delegates to
        # today.
        self._connection = ConnectionActions(
            self._session,
            self._navigator,
            lambda username: self.scrape_person(username, {"main_profile"}),
        )
        # The page reader is a service under the job workflows rather than a
        # peer of them, so it is built here and handed down rather than
        # resolved from the facade later.
        self._job_pages = JobPageReader(self._session, self._navigator, self._content)
        self._jobs = JobScraper(self._navigator, self._capture, self._job_pages)
        self._page = page

    @staticmethod
    def _single_section_result(
        url: str,
        section_name: str,
        text: str,
        references: list[Reference] | None = None,
    ) -> dict[str, Any]:
        """Build a standard single-section scraping response."""
        result: dict[str, Any] = {"url": url, "sections": {}}
        if text:
            result["sections"][section_name] = text
            if references:
                result["references"] = {section_name: references}
        return result

    # ------------------------------------------------------------------
    # Generic browser helpers for LLM-driven connection flow
    # ------------------------------------------------------------------

    async def get_page_text(self) -> str:
        """Extract innerText from the main content area of the current page."""
        text = await self._page.evaluate(
            "() => (document.querySelector('main') || document.body).innerText || ''"
        )
        return strip_linkedin_noise(text) if isinstance(text, str) else ""

    async def click_button_by_text(
        self, text: str, *, scope: str = "main", timeout: int = 5000
    ) -> bool:
        """Click the first button/link whose visible text is exactly *text*.

        Uses a regex filter for exact matching to avoid substring false
        positives (e.g. "Connect" matching "connections").
        Returns True if clicked, False if no match found.
        """
        matches = (
            self._page.locator(scope)
            .locator("button, a, [role='button']")
            .filter(has_text=re.compile(rf"^{re.escape(text)}$"))
        )
        count = await matches.count()
        logger.debug("click_button_by_text(%r): %d matches in %s", text, count, scope)
        if count == 0:
            return False
        target = matches.first
        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            logger.debug("Scroll failed for button '%s'", text, exc_info=True)
        try:
            await target.click(timeout=timeout)
            return True
        except Exception:
            logger.debug("Click failed for button '%s'", text, exc_info=True)
            return False

    async def _locator_is_visible(self, selector: str, *, timeout: int = 2000) -> bool:
        """Return whether the first matching locator is visible."""
        locator = self._page.locator(selector)
        try:
            if await locator.count() == 0:
                return False
        except Exception:
            return False

        first = locator.first
        try:
            await first.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False
        except Exception:
            try:
                return bool(await first.is_visible())
            except Exception:
                return False

    async def _click_first(self, selector: str, *, timeout: int = 5000) -> None:
        """Click the first visible locator that matches a selector."""
        target = self._page.locator(selector).first
        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            logger.debug("Could not scroll %s into view", selector, exc_info=True)
        await target.click(timeout=timeout)

    async def _wait_for_main_text(
        self,
        *,
        minimum_length: int = 100,
        timeout: int = 10000,
        log_context: str,
    ) -> None:
        """Wait for main content to populate enough text to scrape."""
        try:
            await self._page.wait_for_function(
                """({ minimumLength }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > minimumLength;
                }""",
                arg={"minimumLength": minimum_length},
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("%s content did not appear", log_context)

    async def _scroll_main_scrollable_region(
        self,
        *,
        position: Literal["top", "bottom"],
        attempts: int,
        pause_time: float = 0.5,
    ) -> None:
        """Scroll the largest scrollable region inside main when one exists."""
        for _ in range(attempts):
            await self._page.evaluate(
                """({ position }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;

                    const isScrollable = element => {
                        const style = window.getComputedStyle(element);
                        return (
                            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            element.scrollHeight > element.clientHeight + 20
                        );
                    };

                    const candidates = [main, ...main.querySelectorAll('*')].filter(isScrollable);
                    const target = candidates.sort(
                        (left, right) => right.scrollHeight - left.scrollHeight
                    )[0] || main;
                    target.scrollTop = position === 'top' ? 0 : target.scrollHeight;
                    return true;
                }""",
                {"position": position},
            )
            await asyncio.sleep(pause_time)

    async def extract_feed(
        self,
        num_posts: int = 10,
    ) -> ExtractedSection:
        """Scrape the LinkedIn home feed, scrolling until *num_posts* are loaded."""
        return await self._feed.extract_feed(num_posts)

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Navigate to a URL, scroll to load lazy content, and extract innerText."""
        return await self._capture.extract_page(url, section_name, max_scrolls)

    async def scrape_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections."""
        return await self._person.scrape_person(
            username,
            requested,
            callbacks,
            max_scrolls,
            main_profile_already_loaded=main_profile_already_loaded,
            allow_self_alias=allow_self_alias,
        )

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Scrape the authenticated user's own LinkedIn profile."""
        return await self._person.get_my_profile(sections, callbacks, max_scrolls)

    async def connect_with_person(
        self,
        username: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Send a LinkedIn connection request or accept an incoming one."""
        return await self._connection.connect_with_person(username, note=note)

    async def get_sidebar_profiles(self, username: str) -> dict[str, Any]:
        """Extract profile links from sidebar sections on a profile page."""
        return await self._person.get_sidebar_profiles(username)

    async def _read_profile_message_target(self) -> _ProfileMessageTargetResolution:
        """Resolve one recipient-specific top-card compose action after settling."""
        try:
            await self._page.wait_for_function(
                _PROFILE_MESSAGE_TARGET_READY_JS,
                timeout=_PROFILE_MESSAGE_TARGET_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the profile Message action", exc_info=True)

        try:
            data = await self._page.evaluate(_PROFILE_MESSAGE_TARGET_JS)
        except Exception:
            logger.debug("Could not inspect the profile Message action", exc_info=True)
            return _ProfileMessageTargetResolution("failed")
        if not isinstance(data, dict):
            return _ProfileMessageTargetResolution("failed")
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
        with anyio.move_on_after(
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
            with anyio.move_on_after(
                _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
            ) as dom_scope:
                try:
                    await owner.evaluate(_MESSAGE_COMPOSER_DISPOSE_JS)
                except Exception:
                    logger.debug("Could not clear pinned message nodes", exc_info=True)
            if dom_scope.cancel_called:
                logger.warning("Timed out clearing pinned message nodes")
        finally:
            with anyio.move_on_after(
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
    ) -> bool:
        """Wait for one message-list node to gain a different opaque event ID.

        The observer accepts only a node inserted after it was installed whose
        exact visible message unit equals the typed text. That same connected
        node must then change from one non-empty ``data-event-urn`` value to a
        different non-empty value. Every timeout, remount, replacement or
        ambiguity answers "not observed" because submission already happened.
        """
        try:
            await self._page.wait_for_function(
                _MESSAGE_CONFIRMATION_READY_JS,
                arg={
                    **self._message_target_argument(target),
                    "expected": message,
                    "owner": owner,
                    "token": confirmation,
                },
            )
            return True
        except Exception:
            logger.debug("Message send could not be confirmed", exc_info=True)
            return False

    async def _dispose_message_confirmation(
        self, owner: Any, confirmation: str
    ) -> None:
        """Disconnect a request-local confirmation observer."""
        with anyio.move_on_after(
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

    @staticmethod
    def _extract_thread_id(url: str) -> str | None:
        """Parse a LinkedIn thread id from a messaging thread URL."""
        match = re.search(r"/messaging/thread/([^/?#]+)/", url)
        return match.group(1) if match else None

    async def _resolve_conversation_thread_urls(self, display_name: str) -> list[str]:
        """Return all thread URLs whose participant name matches display_name.

        Enumerates the plain messaging inbox (`/messaging/`) plus click-to-capture
        because LinkedIn renders the messaging sidebar with no anchor hrefs, no
        data-thread attributes, and no embedded URNs — clicking each row and
        reading the resulting SPA URL is the only available extraction path.
        The inbox is used rather than `?searchTerm=` because LinkedIn's
        messaging search frequently returns "We didn't find anything" for a
        participant whose thread is plainly present in the inbox (issue #434).
        ``name_filter`` is passed to the enumerator so only the matching row is
        clicked — clicking a row may mark it read, so unrelated threads stay
        untouched.

        Matches by case-insensitive equality on the cleaned participant name
        derived from the row's aria-label, which tolerates duplicate threads
        with the same participant. Browser locale is forced to en-US so the
        verb prefix strips reliably; in any other locale the comparison fails
        cleanly with "Could not find a conversation" rather than returning
        a wrong-thread match. If the inbox scan finds nothing (a thread buried
        below the scrolled rows), it falls back to the `?searchTerm=` search as
        a last resort.

        For a participant with multiple threads, the returned set — and thus
        ``index`` selection in the caller — covers the threads visible in the
        scanned inbox; the search fallback only runs when the inbox scan is
        empty. Open a buried duplicate thread directly via ``thread_id``
        (enumerate IDs with ``search_conversations``).
        """
        target_name = display_name.strip().lower()

        def _match(refs: list[Reference]) -> list[str]:
            # name_filter already gated the clicks; this enforces the same
            # exact-equality match Python-side and tolerates duplicate threads.
            return [
                f"https://www.linkedin.com{ref['url']}"
                for ref in refs
                if (ref.get("text") or "").strip().lower() == target_name
            ]

        # Primary path: enumerate the plain inbox. Reliable for the recent
        # threads that the verify-after-send workflow needs (issue #434).
        await self._navigator._navigate_to_page("https://www.linkedin.com/messaging/")
        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Messaging inbox")
        await handle_modal_close(self._page)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=2, pause_time=0.5
        )
        urls = _match(
            await self._extract_conversation_thread_refs(
                limit=None, context="inbox", name_filter=display_name
            )
        )
        if urls:
            return urls

        # Fallback: LinkedIn's messaging search. Unreliable (often returns
        # "We didn't find anything" even for present threads, see #434), so it
        # runs only when the inbox scan came up empty — e.g. a thread buried
        # below the scrolled inbox window.
        await self._navigator._navigate_to_page(
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(display_name)}"
        )
        await detect_rate_limit(self._page)
        await handle_modal_close(self._page)
        await self._wait_for_main_text(log_context="Messaging search results")
        return _match(
            await self._extract_conversation_thread_refs(
                limit=None, context="search", name_filter=display_name
            )
        )

    async def _open_conversation_by_username(
        self, linkedin_username: str, index: int = 0
    ) -> None:
        """Open the ``index``-th conversation thread for the named participant.

        ``index`` is 0-based and orders threads as the search-results sidebar
        renders them (LinkedIn surfaces newest activity first).
        """
        if index < 0:
            raise LinkedInScraperException(f"index must be non-negative (got {index}).")

        linkedin_username = normalize_person_identifier(linkedin_username)
        profile_url = person_profile_url(linkedin_username, "/")
        await self._navigator._navigate_to_page(profile_url)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        await handle_modal_close(self._page)
        display_name = await self._profile_page._read_profile_display_name()
        if not display_name:
            raise LinkedInScraperException(
                f"Could not resolve a display name for {linkedin_username}."
            )

        try:
            thread_urls = await self._resolve_conversation_thread_urls(display_name)
            if not thread_urls:
                raise LinkedInScraperException(
                    f"Could not find a conversation for {linkedin_username}."
                )
            if index >= len(thread_urls):
                raise LinkedInScraperException(
                    f"index {index} out of range: only {len(thread_urls)} "
                    f"thread(s) exist for {linkedin_username}."
                )

            await self._navigator._navigate_to_page(thread_urls[index])
        except PlaywrightTimeoutError as exc:
            raise LinkedInScraperException(
                "Messaging search results did not load in time."
            ) from exc

    async def scrape_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections."""
        return await self._company.scrape_company(company_name, requested, callbacks)

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the /people/ page."""
        return await self._company.get_company_employees(company_name, keywords)

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting."""
        return await self._jobs.scrape_job(job_id)

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction."""
        return await self._jobs.search_jobs(
            keywords,
            location,
            max_pages,
            date_posted,
            job_type,
            experience_level,
            work_type,
            easy_apply,
            sort_by,
            tool_timeout,
        )

    async def get_saved_jobs(self, max_pages: int = 3) -> dict[str, Any]:
        """List the authenticated user's saved job postings."""
        return await self._jobs.get_saved_jobs(max_pages)

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page."""
        return await self._person.search_people(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
        )

    async def search_companies(
        self,
        keywords: str,
    ) -> dict[str, Any]:
        """Search for companies and extract the results page."""
        return await self._company.search_companies(keywords)

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search LinkedIn posts/content and extract the results page.

        Reproduces the LinkedIn "Posts" content-search tab — the surface for
        catching informal "we're hiring" / "Buscamos ..." posts before a
        formal job listing exists.

        Args:
            keywords: Free-text query (e.g. "Buscamos Unity", "estamos contratando").
            date_posted: Optional recency filter, one of the keys of
                ``search_urls.CONTENT_DATE_POSTED_MAP``. Invalid values raise
                ``FilterValidationError`` (a ``ValueError`` subclass) rather
                than reaching LinkedIn, which would ignore them silently and
                return unfiltered results that look filtered.
            max_pages: Scroll depth, expressed in result "pages" of roughly
                ``_CONTENT_SCROLLS_PER_REQUESTED_PAGE`` scrolls each (default
                3). Content search is an infinite scroll with no per-page URL,
                so this caps how far the page is scrolled rather than fetching
                discrete ``&start=`` pages.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs) and ``section_errors``.
            Verified live: the results page carries no per-post permalink
            anchors, so a post is addressable only through its author.
            The LLM should parse the raw text to extract each post's author,
            headline, body, date, and reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self.extract_page(
            url, section_name="search_results", max_scrolls=max_scrolls
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_inbox(self, limit: int = 20) -> dict[str, Any]:
        """List recent conversations from the messaging inbox."""
        url = "https://www.linkedin.com/messaging/"
        await self._navigator._navigate_to_page(url)
        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Messaging inbox")
        await handle_modal_close(self._page)

        scrolls = max(1, limit // 10)
        await self._scroll_main_scrollable_region(
            position="bottom", attempts=scrolls, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "inbox") if cleaned else []
        )

        # LinkedIn's conversation sidebar uses JS click handlers instead of
        # <a> tags, so anchor extraction cannot capture thread IDs.  Click each
        # conversation item and read the resulting SPA URL to build references.
        conversation_refs = await self._extract_conversation_thread_refs(
            limit=limit, context="inbox"
        )
        if conversation_refs:
            references = dedupe_references(conversation_refs + references)

        return self._single_section_result(
            url,
            "inbox",
            cleaned,
            references=references,
        )

    async def _extract_conversation_thread_refs(
        self, limit: int | None, context: str, *, name_filter: str | None = None
    ) -> list[Reference]:
        """Click each visible conversation item and capture the thread URL.

        Works for both the inbox sidebar and the URL-driven search-results
        sidebar (`/messaging/?searchTerm=…`), which share the same DOM shape:
        each conversation row is an ``<li>`` containing a ``<label>`` with an
        ``aria-label`` attribute carrying the participant name.

        LinkedIn renders the sidebar with no ``<a href>`` tags, no
        ``data-thread-id`` attributes, and no embedded URNs — clicking each
        row and reading the SPA URL is the only reliable extraction path.
        Pass ``limit=None`` to capture every visible row.

        When ``name_filter`` is provided, every row's aria-label is still read
        but only rows whose cleaned participant name equals it (case-insensitive)
        are clicked; non-matching rows are skipped without clicking. Clicking a
        row may mark it as read, so the filter keeps the read-marking side effect
        scoped to the requested participant when resolving by username.
        """
        # The conversation list mounts after main text settles, so wait
        # explicitly for at least one label rather than relying on
        # _wait_for_main_text alone (which only checks chrome text). LinkedIn
        # routinely takes several seconds to hydrate the messaging sidebar
        # after a navigation; an empty sidebar (zero matches) returns on
        # timeout.
        #
        # Selector is structural (`main li label[aria-label]`) rather than
        # text-prefix-based (`aria-label^="Select conversation"`) so it
        # survives any LinkedIn locale — the verb in the aria-label is
        # locale-dependent, the attribute's presence inside a list-item label
        # is not.
        #
        # Wait on `state="attached"` instead of the default `visible`:
        # Ember-managed labels are reliably attached but Playwright's
        # visibility heuristic doesn't always consider them visible.
        try:
            await self._page.wait_for_selector(
                "main li label[aria-label]",
                state="attached",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            logger.debug(
                "conversation labels did not appear within 10s (context=%s)",
                context,
            )
            return []

        # The Ember click handler lives on an inner div; the <li> and <label>
        # don't trigger SPA navigation.  No role/aria attributes exist on the
        # clickable element, so class-name selectors are unavoidable here.
        # The aria-label value flows through unmodified — Python strips any
        # known locale prefix to derive a clean participant name for refs.
        conversations: list[dict[str, str]] = await self._page.evaluate(
            """async ({ limit, nameFilter }) => {
                const labels = Array.from(document.querySelectorAll(
                    'main li label[aria-label]'
                ));
                const cap = (limit == null)
                    ? labels.length
                    : Math.min(labels.length, limit);
                // Normalize the optional participant filter the same way the
                // Python prefix-strip does (en-US "Select conversation with"
                // verb, collapsed whitespace) so the JS-side comparison
                // matches. Only the matching row is clicked — clicking marks a
                // row read, so unrelated threads must not be clicked.
                const wanted = (nameFilter || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                const results = [];
                for (let i = 0; i < cap; i++) {
                    const label = labels[i];
                    const ariaLabel = label.getAttribute('aria-label') || '';
                    const rowName = ariaLabel
                        .replace(/^Select conversation with\\s+/i, '')
                        .replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (wanted && rowName !== wanted) continue;
                    const clickTarget = label.closest('li')
                        ?.querySelector('div[class*="listitem__link"]');
                    if (!clickTarget) continue;
                    const before = location.href;
                    clickTarget.click();
                    // Poll for the SPA URL to settle on the thread route. The
                    // Ember click handler can take a moment to bind after the
                    // label mounts, and a fixed sleep races the initial click.
                    let after = before;
                    for (let waits = 0; waits < 12; waits++) {
                        await new Promise(r => setTimeout(r, 100));
                        after = location.href;
                        if (after !== before
                            && /\\/messaging\\/thread\\//.test(after)) break;
                    }
                    const match = after.match(
                        /\\/messaging\\/thread\\/([^/?#]+)/
                    );
                    if (match) {
                        results.push({ ariaLabel, threadId: match[1] });
                    }
                }
                return results;
            }""",
            {"limit": limit, "nameFilter": name_filter},
        )
        refs: list[Reference] = []
        for conv in conversations:
            ref: Reference = {
                "kind": "conversation",
                "url": f"/messaging/thread/{conv['threadId']}/",
                "context": context,
            }
            name = self._strip_select_conversation_prefix(conv.get("ariaLabel", ""))
            if name:
                ref["text"] = name
            refs.append(ref)
        return refs

    # Best-effort prefix strip for the en-US "Select conversation with " verb.
    # Browser locale is forced to en-US (see BrowserManager) so this normally
    # succeeds; the regex falls through silently for any other locale, in
    # which case the full aria-label flows into the ref's text field rather
    # than a stripped name.
    _SELECT_CONVERSATION_PREFIX_RE = re.compile(
        r"^Select conversation with\s+", re.IGNORECASE
    )

    @classmethod
    def _strip_select_conversation_prefix(cls, aria_label: str) -> str:
        return cls._SELECT_CONVERSATION_PREFIX_RE.sub("", aria_label).strip()

    async def get_conversation(
        self,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: int = 0,
    ) -> dict[str, Any]:
        """Read a specific messaging conversation by thread ID or username.

        ``index`` (0-based) selects which thread to open when a participant has
        multiple conversation threads — e.g. an organic 1-on-1 plus a separate
        InMail. Ignored when ``thread_id`` is provided. Use
        ``search_conversations`` to enumerate thread IDs first if disambiguation
        by index is impractical.

        Side effect when looked up by username: resolution enumerates the
        messaging inbox and click-visits only the row(s) matching the
        participant's display name to capture the thread ID (no anchor hrefs or
        thread-id attributes exist in the sidebar). Each visit selects the row
        in the LinkedIn UI and may mark it as read. Pass ``thread_id`` directly
        to skip this enumeration.
        """
        if not linkedin_username and not thread_id:
            raise LinkedInScraperException(
                "Provide at least one of linkedin_username or thread_id"
            )

        if thread_id:
            thread_id = normalize_thread_id(thread_id)
            await self._navigator._navigate_to_page(
                messaging_thread_url(thread_id, "/")
            )
        else:
            await self._open_conversation_by_username(
                linkedin_username or "", index=index
            )

        await detect_rate_limit(self._page)
        await self._wait_for_main_text(log_context="Conversation")
        await handle_modal_close(self._page)
        await self._scroll_main_scrollable_region(
            position="top", attempts=3, pause_time=0.5
        )

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        # Conversation chrome first: a sidebar preview containing a generic
        # noise marker would otherwise truncate the page before the thread
        # markers are ever seen.
        cleaned = strip_conversation_chrome(raw) if raw else ""
        cleaned = strip_linkedin_noise(cleaned) if cleaned else ""
        references = (
            build_references(raw_result["references"], "conversation")
            if cleaned
            else []
        )
        return self._single_section_result(
            self._page.url,
            "conversation",
            cleaned,
            references=references,
        )

    async def search_conversations(
        self, keywords: str, limit: int = 20
    ) -> dict[str, Any]:
        """Search messages by keyword.

        Uses LinkedIn's ``?searchTerm=`` URL parameter to drive the search
        rather than typing into the searchbox — the URL form is reliable
        regardless of how soon the messaging SPA mounts its searchbox role,
        and (critically) preserves the search filter across click-to-capture
        navigations so per-thread refs can be enumerated.

        ``limit`` caps how many search-result rows the click-to-capture loop
        visits. Each visit selects the row in LinkedIn's UI (and may mark it
        as read), so a low cap is preferable for noisy queries.
        """
        search_url = (
            f"https://www.linkedin.com/messaging/?searchTerm={quote_plus(keywords)}"
        )
        await self._navigator._navigate_to_page(search_url)
        await detect_rate_limit(self._page)
        await handle_modal_close(self._page)
        await self._wait_for_main_text(log_context="Messaging search")

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]
        cleaned = strip_linkedin_noise(raw) if raw else ""
        references: list[Reference] = (
            build_references(raw_result["references"], "search_results")
            if cleaned
            else []
        )

        # Same click-to-capture path as get_inbox: LinkedIn's search sidebar
        # has no anchor hrefs or thread-id attributes, so the only way to
        # surface per-result thread IDs is to click each row and read the SPA
        # URL. URL-driven search keeps the filter active across clicks.
        conversation_refs = await self._extract_conversation_thread_refs(
            limit=limit, context="search_results"
        )
        if conversation_refs:
            references = dedupe_references(conversation_refs + references)

        return self._single_section_result(
            self._page.url,
            "search_results",
            cleaned,
            references=references,
        )

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
        linkedin_username = normalize_person_identifier(linkedin_username)
        profile_url = person_profile_url(linkedin_username, "/")

        await self._navigator._navigate_to_page(profile_url)
        await detect_rate_limit(self._page)

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        resolution = await self._read_profile_message_target()
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
        await self._navigator._navigate_to_page(target.compose_url)
        expected_route = self._page.url
        if not _message_page_url_is_safe(expected_route, target.profile_urn):
            return contracts.message_action_result(
                expected_route,
                "recipient_resolution_failed",
                "LinkedIn opened an unexpected messaging URL.",
            )

        await detect_rate_limit(self._page)
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

                try:
                    try:
                        # A click can dispatch before the evaluate call reports an
                        # error, so an exception from this round trip is ambiguous.
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
                            "The message submission was interrupted and LinkedIn did "
                            "not confirm the send. Check the conversation before "
                            "retrying; retrying may deliver the message twice.",
                            recipient_selected=recipient_selected,
                            retry_safe=False,
                        )

                    if submission != "clicked":
                        may_have_submitted = False
                        return contracts.message_action_result(
                            self._page.url,
                            "send_unavailable",
                            "The local submit path was missing, disabled, or ambiguous.",
                            recipient_selected=recipient_selected,
                        )

                    confirmed = await self._message_send_confirmed(
                        message,
                        target=target,
                        owner=owner,
                        confirmation=confirmation,
                    )
                    if not confirmed:
                        return contracts.message_action_result(
                            self._page.url,
                            "send_unconfirmed",
                            "The message was submitted but LinkedIn did not confirm "
                            "the message-list transition in time. Check the "
                            "conversation before retrying; retrying may deliver the "
                            "message twice.",
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
            # Cancellation only. FastMCP runs the tool inside
            # `anyio.fail_after()` and a cancelled scope discards whatever it
            # returns, so the answer the branch above gives cannot be given
            # here and the log line is all that is left.
            #
            # Silent before explicit submission: nothing can have left yet,
            # and a warning about duplicate delivery would be false.
            if may_have_submitted:
                logger.warning(contracts.SEND_INTERRUPTED_WARNING)
            raise
