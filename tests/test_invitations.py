from linkedin_mcp_server.scraping.invitations import (
    find_invitation,
    invitation_id_to_username,
    invitation_matches,
)


class TestInvitationHelpers:
    def test_invitation_matches_by_id(self):
        invitation = {
            "id": "urn:li:fs_invitation:123",
            "profile_url": "/in/jane-doe/",
        }
        assert invitation_matches(invitation, "urn:li:fs_invitation:123")

    def test_invitation_matches_by_profile_url(self):
        invitation = {
            "id": "jane-doe",
            "profile_url": "/in/jane-doe/",
        }
        assert invitation_matches(invitation, "/in/jane-doe/")

    def test_invitation_matches_by_username(self):
        invitation = {
            "id": "jane-doe",
            "profile_url": "/in/jane-doe/",
        }
        assert invitation_matches(invitation, "jane-doe")

    def test_find_invitation_returns_match(self):
        invitations = [
            {"id": "one", "profile_url": "/in/one/"},
            {"id": "two", "profile_url": "/in/two/"},
        ]
        match = find_invitation(invitations, "two")
        assert match is not None
        assert match["id"] == "two"

    def test_invitation_id_to_username(self):
        assert invitation_id_to_username("jane-doe") == "jane-doe"
        assert invitation_id_to_username("/in/jane-doe/") == "jane-doe"
        assert invitation_id_to_username("urn:li:fs_invitation:123") is None
