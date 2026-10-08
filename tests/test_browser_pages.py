"""Browser-facing helpers exercised with fake Playwright pages (no browser is ever launched)."""

import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from quantcheck import picks_check, picks_report
from quantcheck.picks_check import dismiss_screenshot_overlays


class FakeLocator:
    def __init__(self, count=0):
        self._count = count
        self.clicked = False

    def count(self):
        return self._count

    def click(self, timeout=None):
        self.clicked = True


class FakeDialog:
    def __init__(self, visible=True, close_count=1):
        self.visible = visible
        self.close = FakeLocator(close_count)

    def count(self):
        return 1 if self.visible else 0

    def get_by_role(self, role, name=None):
        return self.close

    def locator(self, selector):
        return self.close


class FakeOverlayPage:
    def __init__(self, dialog):
        self.dialog = dialog
        self.waited = False

    def get_by_role(self, role, name=None):
        return self.dialog

    def wait_for_timeout(self, milliseconds):
        self.waited = True


class FakeCookieContext:
    def cookies(self, base):
        return [{"name": "__Secure-authjs.session-token"}]


class FakeSubscriptionGatePage:
    """Auth cookie present and pick text visible, but the page still says subscribe."""

    context = FakeCookieContext()

    def locator(self, selector):
        return FakeLocator()

    def get_by_role(self, role, name=None):
        return FakeLocator()

    def evaluate(self, script):
        return True


class ScreenshotOverlayTests(unittest.TestCase):
    def test_closes_latest_holdings_tour_before_capture(self):
        dialog = FakeDialog()
        page = FakeOverlayPage(dialog)

        dismiss_screenshot_overlays(page)

        self.assertTrue(dialog.close.clicked)
        self.assertTrue(page.waited)

    def test_does_nothing_when_tour_is_absent(self):
        dialog = FakeDialog(visible=False)
        page = FakeOverlayPage(dialog)

        dismiss_screenshot_overlays(page)

        self.assertFalse(dialog.close.clicked)
        self.assertFalse(page.waited)


class CaptureScreenshotsTests(unittest.TestCase):
    def test_already_logged_in_session_screenshots_each_requested_page_without_a_browser(self):
        class Page:
            def __init__(self):
                self.visited, self.shots = [], []

            def goto(self, url, **kwargs):
                self.visited.append(url)

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, ms):
                pass

            def screenshot(self, path, full_page):
                self.shots.append(path)

        page = Page()
        context = types.SimpleNamespace(new_page=lambda: page, close=lambda: None)
        browser = types.SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)
        playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda headless: browser))

        class Manager:
            def __enter__(self):
                return playwright

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(picks_check, "SHOTS", Path(tmp)), \
             patch.object(picks_check, "load_env", return_value={}), \
             patch.object(picks_check, "sync_playwright", lambda: Manager()), \
             patch.object(picks_check, "_wait_for_screenshot_ready"), \
             patch.object(picks_check, "dismiss_screenshot_overlays"), \
             patch.object(picks_check.report, "has_auth_session", return_value=True), \
             patch.object(picks_check.report, "has_picks_content", return_value=True), \
             patch.object(picks_check.report, "is_login_prompt_visible", return_value=False):
            shots = picks_check.capture_logged_in_screenshots(["monthly", "weekly"])

        self.assertEqual(set(shots), {"monthly", "weekly"})
        self.assertTrue(shots["monthly"].name.startswith("portfolio_"))
        self.assertTrue(shots["weekly"].name.startswith("watchlist_"))
        self.assertEqual(sorted(Path(p).name[:9] for p in page.shots), ["portfolio", "watchlist"])


class PageValidationTests(unittest.TestCase):
    def test_has_auth_session_checks_the_session_cookie_and_never_raises(self):
        class NoCookies:
            context = types.SimpleNamespace(cookies=lambda base: [{"name": "other"}])

        class Broken:
            @property
            def context(self):
                raise RuntimeError("page closed")

        self.assertTrue(picks_report.has_auth_session(FakeSubscriptionGatePage()))
        self.assertFalse(picks_report.has_auth_session(NoCookies()))
        self.assertFalse(picks_report.has_auth_session(Broken()))

    def test_subscription_gate_is_rejected_even_with_auth_cookie_and_pick_text(self):
        with self.assertRaisesRegex(RuntimeError, "subscription/paywall"):
            picks_report.assert_authenticated_page(FakeSubscriptionGatePage(), "weekly")

    def test_watchlist_dialog_paywall_detection(self):
        self.assertTrue(picks_report.is_watchlist_dialog_paywalled("SNDK Subscriber-only pick Subscribe to unlock the full watchlist"))
        self.assertFalse(picks_report.is_watchlist_dialog_paywalled("SNDK Sandisk Corporation Momentum 1.96/2 Relative Strength 3.00/3"))


if __name__ == "__main__":
    unittest.main()
