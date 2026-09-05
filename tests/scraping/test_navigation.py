"""Tests for the page navigation lifecycle owner."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from patchright.async_api import Error as PatchrightError

import pytest

from linkedin_mcp_server.core.exceptions import ProxyConnectionError
from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from .support.navigation import navigate


class TestNavigationDiagnostics:
    async def test_goto_with_auth_checks_clicks_remember_me_and_retries(
        self, mock_page
    ):
        navigator = PageNavigator(ScrapingSession(mock_page))

        async def goto_side_effect(*args, **kwargs):
            if mock_page.goto.await_count == 1:
                raise Exception("net::ERR_TOO_MANY_REDIRECTS")
            return None

        mock_page.goto = AsyncMock(side_effect=goto_side_effect)

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True],
            ) as mock_resolve,
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert mock_page.goto.await_count == 2
        mock_resolve.assert_awaited_once()

    async def test_goto_with_auth_checks_unhooks_outer_listener_before_retry(
        self, mock_page
    ):
        navigator = PageNavigator(ScrapingSession(mock_page))
        listener_events: list[str] = []

        def record_on(event_name, callback):
            listener_events.append(f"on:{event_name}")

        def record_remove(event_name, callback):
            listener_events.append(f"off:{event_name}")

        mock_page.on.side_effect = record_on
        mock_page.remove_listener.side_effect = record_remove

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                side_effect=["account picker", None],
            ),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert listener_events == [
            "on:framenavigated",
            "off:framenavigated",
            "on:framenavigated",
            "off:framenavigated",
        ]

    async def test_goto_with_auth_checks_records_original_failure_before_retry(
        self, mock_page
    ):
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(
            side_effect=[
                Exception("net::ERR_TOO_MANY_REDIRECTS"),
                Exception("retry failed"),
            ]
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True, False],
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.record_page_trace",
                new_callable=AsyncMock,
            ) as mock_trace,
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(Exception, match="retry failed"),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        trace_steps = [call.args[1] for call in mock_trace.await_args_list]
        assert "extractor-navigation-error-before-remember-me-retry" in trace_steps

        trace_call = next(
            call
            for call in mock_trace.await_args_list
            if call.args[1] == "extractor-navigation-error-before-remember-me-retry"
        )
        assert (
            trace_call.kwargs["extra"]["error"]
            == "Exception: net::ERR_TOO_MANY_REDIRECTS"
        )

    async def test_a_hop_on_the_way_reaches_the_failure_log(self, mock_page):
        """Where a failed navigation went is the diagnostic it leaves behind.

        The address is read off the frame the event carries and not off the
        page, so a double whose frame never moves records nothing while
        looking exactly like one that works.
        """
        navigator = PageNavigator(ScrapingSession(mock_page))
        checkpoint = "https://www.linkedin.com/checkpoint/challenge/"

        async def goto_then_fail(*args, **kwargs):
            navigate(mock_page, checkpoint)
            raise Exception("net::ERR_ABORTED")

        mock_page.goto = AsyncMock(side_effect=goto_then_fail)

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                navigator,
                "_log_navigation_failure",
                new_callable=AsyncMock,
            ) as mock_log_failure,
            pytest.raises(Exception, match="ERR_ABORTED"),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        logged = mock_log_failure.await_args
        assert logged is not None
        assert logged.args[3] == [checkpoint]

    async def test_goto_with_auth_checks_logs_failure_context(self, mock_page):
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                navigator,
                "_log_navigation_failure",
                new_callable=AsyncMock,
            ) as mock_log_failure,
            pytest.raises(Exception, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        mock_log_failure.assert_awaited_once()
        mock_page.on.assert_called_once()
        mock_page.remove_listener.assert_called_once()


class TestWatchingNavigations:
    def test_records_main_frame_hops_without_deduplicating_and_cleans_up(
        self, mock_page
    ):
        navigator = PageNavigator(ScrapingSession(mock_page))

        with navigator._watching_navigations() as hops:
            navigate(mock_page)
            navigate(mock_page)
            for callback in list(mock_page.listeners["framenavigated"]):
                callback(object())

        assert hops == [mock_page.url, mock_page.url]
        assert mock_page.listeners["framenavigated"] == []

    def test_cleans_up_when_the_watched_block_raises(self, mock_page):
        navigator = PageNavigator(ScrapingSession(mock_page))

        with pytest.raises(RuntimeError, match="synthetic failure"):
            with navigator._watching_navigations():
                raise RuntimeError("synthetic failure")

        assert mock_page.listeners["framenavigated"] == []


class TestSettleNavigation:
    """The listener decides whether anything happened; the URL cannot."""

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    @staticmethod
    def _sleep(clock, hops, page, schedule=()):
        """Advance the clock per poll, landing each hop at its own moment.

        Each hop replaces the document, which is what a reload and a redirect
        both do. A same-document change is spelled by leaving `time_origin`
        alone instead.
        """
        pending = list(schedule)

        async def sleep(seconds: float) -> None:
            clock.now += seconds
            while pending and pending[0] <= clock.now:
                pending.pop(0)
                hops.append("hop")
                page.time_origin += 1.0

        return sleep

    async def test_a_destroyed_context_reads_as_no_document(self, mock_page):
        """A navigation in flight takes the context the reading needs with it.

        The class patchright raises for that is `Error`, measured, and not a
        `RuntimeError`. A handler narrowed to the latter would turn the
        ordinary case this reading exists for into an unhandled exception,
        so the double is held to the real class.
        """
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.evaluate = AsyncMock(
            side_effect=PatchrightError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation."
            )
        )

        assert await navigator._document_origin() is None

    async def test_a_page_going_nowhere_costs_the_lag_and_not_the_quiet(
        self, mock_page
    ):
        """An ordinary failure has no navigation behind it.

        Charging it the quiet window spends half a second on every DOM error,
        and a call near its tool timeout loses the diagnostic it was about to
        build.
        """
        clock = self.Clock()
        navigator = PageNavigator(ScrapingSession(mock_page))
        hops: list[str] = []

        with (
            patch.object(session_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page),
            ),
        ):
            assert (
                await navigator._settle_navigation(hops, mock_page.time_origin) is False
            )

        assert clock.now < PageNavigator.URL_SETTLE_QUIET
        assert clock.now >= PageNavigator.URL_SETTLE_LAG

    async def test_a_reload_is_a_navigation_though_the_address_holds(self, mock_page):
        """A reload replaces the document and leaves the address alone.

        Comparing addresses calls the replacement the same page, so a picker
        served by a reload was read as search results. The event says so.
        """
        clock = self.Clock()
        navigator = PageNavigator(ScrapingSession(mock_page))
        hops: list[str] = []

        with (
            patch.object(session_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05]),
            ),
        ):
            assert (
                await navigator._settle_navigation(hops, mock_page.time_origin) is True
            )

        assert mock_page.wait_for_load_state.await_count == 1

    async def test_a_chain_is_followed_to_its_last_hop(self, mock_page):
        """Hops are counted, not compared.

        A chain that returns to the route it started on reads as one that
        never left, and its last hop is what decides whether this is a
        checkpoint.
        """
        clock = self.Clock()
        navigator = PageNavigator(ScrapingSession(mock_page))
        hops: list[str] = []

        with (
            patch.object(session_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05, 0.4]),
            ),
        ):
            assert (
                await navigator._settle_navigation(hops, mock_page.time_origin) is True
            )

        assert len(hops) == 2
        assert clock.now >= 0.4 + PageNavigator.URL_SETTLE_QUIET

    async def test_a_history_change_is_not_a_navigation(self, mock_page):
        """LinkedIn rewrites its own address, and the event cannot tell.

        `pushState`, `replaceState` and a hash change each fire
        `framenavigated` on the main frame, and a search page appends
        `currentJobId` that way by itself. Settling on the event alone charges
        every healthy page the quiet window plus a document wait plus the
        barrier check that follows from it. The document surviving is what
        says nothing was replaced.
        """
        clock = self.Clock()
        navigator = PageNavigator(ScrapingSession(mock_page))
        origin = mock_page.time_origin
        navigate(mock_page, same_document=True)
        hops = ["https://www.linkedin.com/jobs/search/?currentJobId=1"]

        with (
            patch.object(session_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page),
            ),
        ):
            assert await navigator._settle_navigation(hops, origin) is False

        assert clock.now >= PageNavigator.URL_SETTLE_LAG
        assert clock.now < PageNavigator.URL_SETTLE_QUIET
        assert mock_page.wait_for_load_state.await_count == 0

    async def test_a_redirect_behind_a_history_change_is_still_caught(self, mock_page):
        """The address is announced before the checkpoint commits.

        A search page names its selected job the moment a card is chosen, and
        a checkpoint arriving right behind it would be waved through by a
        settler that left on the first hop. The wait is for a replaced
        document, so the second hop is what ends it.
        """
        clock = self.Clock()
        navigator = PageNavigator(ScrapingSession(mock_page))
        origin = mock_page.time_origin
        navigate(mock_page, same_document=True)
        hops = ["https://www.linkedin.com/jobs/search/?currentJobId=1"]

        with (
            patch.object(session_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05]),
            ),
        ):
            assert await navigator._settle_navigation(hops, origin) is True

        assert mock_page.wait_for_load_state.await_count == 1


class TestProxyNavigationFailures:
    """A proxy outage during an ordinary tool call is reported as itself."""

    async def test_proxy_error_is_raised_instead_of_a_scraping_failure(self, mock_page):
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(
            side_effect=Exception("net::ERR_PROXY_CONNECTION_FAILED at …")
        )

        with pytest.raises(ProxyConnectionError):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

    async def test_proxy_error_is_converted_before_it_reaches_a_trace(self, mock_page):
        # The trace records the raw exception text, which for a proxy failure
        # can quote the proxy URL and put a password into trace.jsonl.
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(
            side_effect=Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.record_page_trace",
                new_callable=AsyncMock,
            ) as mock_trace,
            pytest.raises(ProxyConnectionError),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        recorded = [call.args[1] for call in mock_trace.await_args_list]
        assert "extractor-navigation-error" not in recorded

    async def test_ordinary_navigation_failure_is_unaffected(self, mock_page):
        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_ABORTED"))

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert not isinstance(excinfo.value, ProxyConnectionError)


class TestNavigationFailureLogRedaction:
    """The navigation-failure log must not carry proxy credentials.

    It reaches the log even for errors the marker check does not recognise as
    proxy faults, and that log is what users paste into issue reports.
    """

    async def test_credentials_are_redacted_from_the_log(
        self, mock_page, monkeypatch, caplog
    ):
        import logging

        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = "acctzone9"
        config.browser.proxy_password = "s3cr3t"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

        navigator = PageNavigator(ScrapingSession(mock_page))
        # No proxy marker, so it is not converted and reaches the logger.
        mock_page.goto = AsyncMock(
            side_effect=Exception(
                "failed via http://acctzone9:s3cr3t@gate.example:7000"
            )
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            caplog.at_level(logging.WARNING),
            pytest.raises(Exception),
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert "s3cr3t" not in caplog.text
        assert "acctzone9" not in caplog.text


class TestNavigationFailureCrossesTheToolBoundaryClean:
    """The re-raised exception itself must be credential-free.

    Redacting the extractor's own trace and log is not enough: everything
    downstream logs the exception too, starting with the catch-all in
    error_handler and FastMCP's handler above it.
    """

    async def test_reraised_exception_carries_no_credentials(
        self, mock_page, monkeypatch
    ):
        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = "acctzone9"
        config.browser.proxy_password = "s3cr3t"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

        navigator = PageNavigator(ScrapingSession(mock_page))
        mock_page.goto = AsyncMock(
            side_effect=Exception(
                "failed via http://acctzone9:s3cr3t@gate.example:7000"
            )
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.navigation.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await navigator._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert "s3cr3t" not in str(excinfo.value)
        assert "acctzone9" not in str(excinfo.value)
        # The raw error must not survive as a cause either: the handlers
        # downstream print the whole chain.
        assert excinfo.value.__cause__ is None
