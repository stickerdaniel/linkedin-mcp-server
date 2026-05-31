"""Helpers for LinkedIn incoming connection invitations."""

from __future__ import annotations

import re
from typing import Any

RECEIVED_INVITATIONS_URL = (
    "https://www.linkedin.com/mynetwork/invitation-manager/received/"
)

EXTRACT_PENDING_INVITATIONS_SCRIPT = """() => {
    const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();

    const parseProfilePath = href => {
        if (!href) return null;
        const match = href.match(/\\/in\\/([A-Za-z0-9_-]+)\\/?/);
        return match ? `/in/${match[1]}/` : null;
    };

    const findInvitationId = root => {
        if (!root) return null;
        const queue = [root];
        while (queue.length) {
            const el = queue.shift();
            if (!el || el.nodeType !== 1) continue;
            for (const attr of el.getAttributeNames()) {
                const lower = attr.toLowerCase();
                const value = el.getAttribute(attr);
                if (!value) continue;
                if (lower === 'data-invitation-id') return value;
                if (lower.includes('invitation') && lower.includes('id')) return value;
            }
            queue.push(...el.children);
        }
        return null;
    };

        if (seen.has(invitationId)) continue;
        seen.add(invitationId);

        let name = '';
        if (profileLink) {
            name = normalize(
                profileLink.querySelector('span[aria-hidden="true"]')?.innerText ||
                profileLink.innerText
            ).split('\\n')[0];
        }
        if (!name) {
            const aria = btn.getAttribute('aria-label') || '';
            const match = aria.match(/Accept (.+?)'?s invitation/i);
            if (match) name = normalize(match[1]);
        }

        let headline = '';
        const subtitle = card?.querySelector('[class*="subtitle"], [class*="subline"]');
        if (subtitle) {
            headline = normalize(subtitle.innerText);
        }
        if (!headline && card) {
            const lines = normalize(card.innerText)
                .split('\\n')
                .map(line => normalize(line))
                .filter(Boolean);
            const nameIdx = lines.findIndex(line => line === name);
            if (nameIdx >= 0 && nameIdx + 1 < lines.length) {
                const candidate = lines[nameIdx + 1];
                if (!['Accept', 'Ignore'].includes(candidate)) headline = candidate;
            }
        }

        cards.push({
            id: invitationId,
            name,
            headline,
            profile_url: profilePath,
        });
    }

    return cards;
}"""

CLICK_ACCEPT_INVITATION_SCRIPT = """async ({ invitationId }) => {
    const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();

    const parseProfilePath = href => {
        if (!href) return null;
        const match = href.match(/\\/in\\/([A-Za-z0-9_-]+)\\/?/);
        return match ? `/in/${match[1]}/` : null;
    };

    const findInvitationId = root => {
        if (!root) return null;
        const queue = [root];
        while (queue.length) {
            const el = queue.shift();
            if (!el || el.nodeType !== 1) continue;
            for (const attr of el.getAttributeNames()) {
                const lower = attr.toLowerCase();
                const value = el.getAttribute(attr);
                if (!value) continue;
                if (lower === 'data-invitation-id') return value;
                if (lower.includes('invitation') && lower.includes('id')) return value;
            }
            queue.push(...el.children);
        }
        return null;
    };

    const matchesId = (candidate, targetId) => {
        if (!candidate || !targetId) return false;
        const needle = targetId.toLowerCase();
        const value = candidate.toLowerCase();
        if (value === needle) return true;
        const profilePath = parseProfilePath(candidate) || candidate;
        if (profilePath.toLowerCase() === needle) return true;
        const username = profilePath.replace(/^\\/in\\//, '').replace(/\\/$/, '').toLowerCase();
        return username === needle;
    };

    const acceptButtons = Array.from(
        document.querySelectorAll('main button, main div[role="button"]')
    ).filter(btn => {
        const aria = normalize(btn.getAttribute('aria-label')).toLowerCase();
        const text = normalize(btn.innerText).toLowerCase();
        if (aria.includes('ignore') || text === 'ignore') return false;
        return aria.includes('accept') || text === 'accept';
    });

    for (const btn of acceptButtons) {
        const card =
            btn.closest('[data-view-name*="invitation"]') ||
            btn.closest('li') ||
            btn.closest('div[data-chameleon-result-urn]') ||
            btn.parentElement?.parentElement?.parentElement?.parentElement;

        const profileLink = card?.querySelector('a[href*="/in/"]');
        const profilePath = parseProfilePath(profileLink?.getAttribute('href'));
        let candidateId = findInvitationId(card) || findInvitationId(btn);
        if (!candidateId) {
            const urnAttr = card?.getAttribute('data-chameleon-result-urn') || '';
            if (urnAttr) candidateId = urnAttr;
        }
        if (!candidateId && profilePath) {
            candidateId = profilePath.replace(/^\\/in\\//, '').replace(/\\/$/, '');
        }

        if (
            !matchesId(candidateId, invitationId) &&
            !(profilePath && matchesId(profilePath, invitationId))
        ) {
            continue;
        }

        btn.click();
        await new Promise(resolve => setTimeout(resolve, 500));
        return true;
    }

    return false;
}"""


def pending_invitations_result(
    url: str,
    *,
    status: str,
    message: str,
    invitations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a structured response for listing pending invitations."""
    return {
        "url": url,
        "status": status,
        "message": message,
        "invitations": invitations or [],
    }


def accept_invitation_result(
    url: str,
    *,
    status: str,
    message: str,
    invitation_id: str,
    name: str = "",
    profile_url: str = "",
) -> dict[str, Any]:
    """Build a structured response for accepting one invitation."""
    result: dict[str, Any] = {
        "url": url,
        "status": status,
        "message": message,
        "invitation_id": invitation_id,
    }
    if name:
        result["name"] = name
    if profile_url:
        result["profile_url"] = profile_url
    return result


def invitation_matches(invitation: dict[str, str], invitation_id: str) -> bool:
    """Return whether an invitation entry matches the requested id."""
    needle = invitation_id.strip().lower()
    if not needle:
        return False

    candidate_id = invitation.get("id", "").lower()
    if candidate_id == needle:
        return True

    profile_url = invitation.get("profile_url", "").lower()
    if profile_url == needle or profile_url == f"/in/{needle}/":
        return True

    username = profile_url.removeprefix("/in/").removesuffix("/").lower()
    return username == needle


def find_invitation(
    invitations: list[dict[str, str]], invitation_id: str
) -> dict[str, str] | None:
    """Find the first invitation matching invitation_id."""
    for invitation in invitations:
        if invitation_matches(invitation, invitation_id):
            return invitation
    return None


def invitation_id_to_username(invitation_id: str) -> str | None:
    """Best-effort username extraction when id is a profile slug."""
    value = invitation_id.strip().strip("/")
    if not value:
        return None
    if value.startswith("in/"):
        value = value.removeprefix("in/")
    if value.startswith("/in/"):
        value = value.removeprefix("/in/")
    value = value.strip("/")
    if re_match_username(value):
        return value
    return None


def re_match_username(value: str) -> bool:
    """Return whether value looks like a LinkedIn vanity username."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))
