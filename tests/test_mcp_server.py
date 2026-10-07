import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quantcheck.mcp_server import server
from quantcheck.membership_store import Member, MembershipStore, save_store
from quantcheck.service.errors import ServiceError

EXPECTED_TOOL_NAMES = {
    "list_members",
    "get_member",
    "add_member",
    "extend_member",
    "set_expiry",
    "remove_member",
    "expiring_report",
    "sync_recipients",
    "migrate_from_recipients",
    "bulk_add_members",
    "bulk_extend_members",
    "bulk_set_expiry",
    "record_payment",
    "payments_report",
    "route_preview",
    "ops_status",
    "ops_run_job",
    "ops_diagnose",
    "ops_logs",
    "ops_recent_deliveries",
    "ops_schedule_preview",
    "ops_historical_resend_preview",
}


def run_async(coro):
    import asyncio

    return asyncio.run(coro)


class ServerStartsAndListsToolsTests(unittest.TestCase):
    def test_lists_exactly_the_expected_tools(self):
        tools = run_async(server.list_tools())
        names = {t.name for t in tools}
        self.assertEqual(names, EXPECTED_TOOL_NAMES)

    def test_every_tool_has_a_nonempty_description(self):
        tools = run_async(server.list_tools())
        for tool in tools:
            self.assertTrue(tool.description and tool.description.strip(), f"{tool.name} has no description")

    def test_confirm_requiring_tool_documents_it(self):
        tools = {t.name: t for t in run_async(server.list_tools())}
        self.assertIn("confirm=true", tools["ops_run_job"].description.lower())

    def test_required_fields_match_service_signatures(self):
        tools = {t.name: t for t in run_async(server.list_tools())}
        self.assertEqual(set(tools["add_member"].input_schema["required"]), {"email"})
        self.assertEqual(set(tools["remove_member"].input_schema["required"]), {"email", "reason"})
        self.assertEqual(set(tools["migrate_from_recipients"].input_schema["required"]), {"expires_at"})
        # set_expiry's `note` has a default at the MCP layer even though the
        # service layer requires it (mirrors admin_cli's DEFAULT_SET_EXPIRY_NOTE).
        self.assertEqual(set(tools["set_expiry"].input_schema["required"]), {"email"})


class ResultEnvelopeShapeTests(unittest.TestCase):
    """Tests the `_call`/`_ok`/`_err` helpers directly: these are what make
    every tool's error path clean, parseable JSON with is_error set
    correctly, instead of the framework's default "Error executing tool
    <name>: <message>" prefix-polluted text.
    """

    def test_success_returns_is_error_false_with_parseable_json(self):
        from quantcheck.mcp_server import _call

        result = _call(lambda: {"hello": "world"})
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), {"hello": "world"})

    def test_service_error_returns_is_error_true_with_clean_envelope(self):
        from quantcheck.mcp_server import _call

        def boom():
            raise ServiceError("member_not_found", "no such member", {"email": "a@example.com"})

        result = _call(boom)
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload, {"error": {"code": "member_not_found", "message": "no such member", "details": {"email": "a@example.com"}}})

    def test_unexpected_exception_returns_is_error_true_with_internal_error_code(self):
        from quantcheck.mcp_server import _call

        def boom():
            raise RuntimeError("something broke")

        result = _call(boom)
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "something broke")

    def test_error_text_is_never_prefixed_by_the_framework(self):
        # This is the specific bug this design avoids: raising ToolError
        # from inside a tool causes the framework to prepend "Error
        # executing tool <name>: " to the message, breaking naive
        # json.loads(). _call must never raise; it returns CallToolResult
        # directly so that wrapping never happens.
        from quantcheck.mcp_server import _call

        result = _call(lambda: (_ for _ in ()).throw(ServiceError("x", "y")))
        self.assertFalse(result.content[0].text.startswith("Error executing tool"))


class ToolCallsAgainstATmpRootTests(unittest.TestCase):
    """service.members/service.ops resolve their root via
    quantcheck.config.get_root(), which reads os.environ fresh on every
    call (unlike notify_routes.ROOT, which freezes at import time) -- so
    patching QUANTCHECK_HOME per-test is sufficient to isolate these calls
    from the real /opt/quantcheck state without needing a root= parameter
    on the MCP tools themselves.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\n", encoding="utf-8")
        self.env_patch = patch.dict("os.environ", {"QUANTCHECK_HOME": str(self.root)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def write_store(self, members):
        save_store(MembershipStore(path=self.root / "state" / "memberships.json", members=list(members)), backup=False)

    def test_list_members_tool_reads_the_tmp_root_store(self):
        from quantcheck.mcp_server import list_members

        self.write_store([Member(email="a@example.com", status="active", joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc), expires_at=None)])

        result = list_members()
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["members"][0]["email"], "a@example.com")

    def test_get_member_not_found_is_a_clean_error(self):
        from quantcheck.mcp_server import get_member

        self.write_store([])
        result = get_member(email="nobody@example.com")
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"]["code"], "member_not_found")

    def test_ops_status_tool_reads_the_tmp_root(self):
        from quantcheck.mcp_server import ops_status

        result = ops_status()
        self.assertFalse(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertIn("lock", payload)

    def test_ops_run_job_confirm_gate_enforced_through_the_tool(self):
        from quantcheck.mcp_server import ops_run_job

        result = ops_run_job(kind="test_email")
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"]["code"], "confirmation_required")

    def test_migrate_from_recipients_defaults_to_dry_run(self):
        from quantcheck.mcp_server import migrate_from_recipients

        (self.root / "notify_recipients.txt").write_text("a@example.com\n", encoding="utf-8")
        result = migrate_from_recipients(expires_at="2026-10-09")
        payload = json.loads(result.content[0].text)
        self.assertTrue(payload["dry_run"])
        self.assertFalse((self.root / "state" / "memberships.json").exists())


class RoutePreviewToolTests(unittest.TestCase):
    def test_delegates_to_notify_routes_route_preview_with_the_given_route(self):
        from quantcheck.mcp_server import route_preview

        fake_result = {"route": "admin", "included": ["admin@example.com"], "excluded": [], "enforcement": True, "counts": {}}
        with patch("quantcheck.mcp_server._route_preview", return_value=fake_result) as mocked:
            result = route_preview(route="admin")
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), fake_result)
        mocked.assert_called_once()

    def test_invalid_route_is_a_clean_error_not_a_crash(self):
        from quantcheck.mcp_server import route_preview

        result = route_preview(route="not_a_real_route")
        self.assertTrue(result.is_error)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["error"]["code"], "invalid_route")



class BulkAndPaymentToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\n", encoding="utf-8")
        (self.root / "notify_recipients.txt").write_text("", encoding="utf-8")
        env_patch = patch.dict("os.environ", {"QUANTCHECK_HOME": str(self.root)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def payload(self, result):
        return json.loads(result.content[0].text)

    def test_tool_schemas(self):
        tools = {t.name: t for t in run_async(server.list_tools())}
        self.assertEqual(set(tools["add_member"].input_schema["required"]), {"email"})
        self.assertEqual(set(tools["extend_member"].input_schema["required"]), {"email", "months"})
        self.assertEqual(set(tools["bulk_add_members"].input_schema["required"]), {"emails"})
        self.assertEqual(set(tools["bulk_extend_members"].input_schema["required"]), {"months"})
        self.assertEqual(set(tools["bulk_set_expiry"].input_schema["required"]), {"expires_at"})
        self.assertEqual(set(tools["record_payment"].input_schema["required"]), {"email", "amount"})
        for name in ("bulk_add_members", "bulk_extend_members", "bulk_set_expiry"):
            self.assertIs(tools[name].input_schema["properties"]["dry_run"]["default"], True, name)

    def test_instructions_mention_bulk_dry_run(self):
        from quantcheck.mcp_server import INSTRUCTIONS

        self.assertIn("bulk_add_members", INSTRUCTIONS)
        self.assertIn("dry_run=false", INSTRUCTIONS)

    def test_bulk_add_defaults_to_dry_run(self):
        from quantcheck.mcp_server import bulk_add_members

        payload = self.payload(bulk_add_members(emails="a@example.com b@example.com", expires_at="2026-11-01"))
        self.assertTrue(payload["dry_run"])
        self.assertFalse((self.root / "state" / "memberships.json").exists())
        self.assertEqual((self.root / "notify_recipients.txt").read_text(encoding="utf-8"), "")

    def test_add_member_align_payment_and_bulk_apply(self):
        from quantcheck.mcp_server import add_member, bulk_add_members, bulk_extend_members, bulk_set_expiry, get_member, payments_report, record_payment

        first = add_member(email="a@example.com", expires_at="2099-01-09", amount=10, currency="CNY", channel="wechat")
        self.assertFalse(first.is_error)
        aligned = self.payload(add_member(email="b@example.com", align=True))
        self.assertEqual(aligned["member"]["expires_at"], "2099-01-09T00:00:00-05:00")
        bad = add_member(email="c@example.com")
        self.assertTrue(bad.is_error)
        self.assertEqual(self.payload(bad)["error"]["code"], "invalid_arguments")

        applied = self.payload(bulk_add_members(emails=["c@example.com", "d@example.com"], align=True, dry_run=False))
        self.assertEqual(applied["counts"]["ok"], 2)
        ext = self.payload(bulk_extend_members(months=1, all_active=True))
        self.assertTrue(ext["dry_run"])
        self.assertEqual(ext["counts"]["ok"], 4)
        sett = self.payload(bulk_set_expiry(expires_at="2099-03-09", emails="a@example.com", dry_run=False))
        self.assertEqual(sett["counts"]["ok"], 1)

        pay = self.payload(record_payment(email="a@example.com", amount=5.5, currency="CNY", channel="alipay"))
        self.assertEqual(pay["member"]["total_paid"], {"CNY": 15.5})
        self.assertEqual(self.payload(get_member(email="a@example.com"))["member"]["expires_at"], "2099-03-09T00:00:00-04:00")
        report = self.payload(payments_report())
        self.assertEqual((report["count"], report["totals_by_currency"]), (2, {"CNY": 15.5}))


if __name__ == "__main__":
    unittest.main()
