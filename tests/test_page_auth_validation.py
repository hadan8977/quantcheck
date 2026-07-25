import unittest

from quantcheck import picks_report


class FakeLocator:
    def count(self):
        return 0


class FakeContext:
    def cookies(self, base):
        return [{"name": "__Secure-authjs.session-token"}]


class FakePage:
    context = FakeContext()

    def locator(self, selector):
        return FakeLocator()

    def get_by_role(self, role, name=None):
        return FakeLocator()

    def evaluate(self, script):
        if "subscription" in script.lower():
            return True
        return True


class PageValidationTests(unittest.TestCase):
    def test_subscription_gate_is_rejected_even_with_auth_cookie_and_pick_text(self):
        with self.assertRaisesRegex(RuntimeError, "subscription/paywall"):
            picks_report.assert_authenticated_page(FakePage(), "weekly")
    def test_watchlist_dialog_subscription_copy_is_detected(self):
        self.assertTrue(picks_report.is_watchlist_dialog_paywalled(
            "SNDK Subscriber-only pick Subscribe to unlock the full watchlist"
        ))

    def test_authenticated_watchlist_dialog_is_not_mistaken_for_paywall(self):
        self.assertFalse(picks_report.is_watchlist_dialog_paywalled(
            "SNDK Sandisk Corporation Momentum 1.96/2 Relative Strength 3.00/3"
        ))


if __name__ == "__main__":
    unittest.main()
