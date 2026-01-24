def test_get_linkedin_cookie_present(monkeypatch):
    monkeypatch.setenv("LINKEDIN_COOKIE", "test_cookie")
    from linkedin_mcp_server.utils import get_linkedin_cookie

    assert get_linkedin_cookie() == "test_cookie"


def test_get_linkedin_cookie_missing(monkeypatch):
    monkeypatch.delenv("LINKEDIN_COOKIE", raising=False)
    from linkedin_mcp_server.utils import get_linkedin_cookie

    assert get_linkedin_cookie() is None
