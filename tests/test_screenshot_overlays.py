import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import unittest

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


class FakePage:
    def __init__(self, dialog):
        self.dialog = dialog
        self.waited = False

    def get_by_role(self, role, name=None):
        return self.dialog

    def wait_for_timeout(self, milliseconds):
        self.waited = True


class ScreenshotOverlayTests(unittest.TestCase):
    def test_closes_latest_holdings_tour_before_capture(self):
        dialog = FakeDialog()
        page = FakePage(dialog)

        dismiss_screenshot_overlays(page)

        self.assertTrue(dialog.close.clicked)
        self.assertTrue(page.waited)

    def test_does_nothing_when_tour_is_absent(self):
        dialog = FakeDialog(visible=False)
        page = FakePage(dialog)

        dismiss_screenshot_overlays(page)

        self.assertFalse(dialog.close.clicked)
        self.assertFalse(page.waited)


if __name__ == '__main__':
    unittest.main()
