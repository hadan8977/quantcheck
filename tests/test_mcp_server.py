"""The MCP server is a thin 1:1 wrapper over quantcheck.service: these tests cover wiring only.

Business rules live in test_service_members.py / test_service_ops.py. Here the
service calls are stubbed wherever the point is "which service call, with which
params"; a few tests run the real service against a tmp root to prove the
dry-run and confirm defaults hold through the tool layer.
"""

import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantcheck import mcp_server
from quantcheck.mcp_server import _call, server
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
STUB = {"stub": True}


def payload(result):
    return json.loads(result.content[0].text)


class ToolListAndSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tools = {t.name: t for t in asyncio.run(server.list_tools())}

    def test_lists_exactly_the_expected_tools_each_with_a_description(self):
        self.assertEqual(set(self.tools), EXPECTED_TOOL_NAMES)
        for name, tool in self.tools.items():
            with self.subTest(name):
                self.assertTrue(tool.description and tool.description.strip())
                self.assertTrue(callable(getattr(mcp_server, name)))  # reachable as a module-level function
        self.assertIn("confirm=true", self.tools["ops_run_job"].description.lower())

    def test_required_fields_and_dry_run_defaults(self):
        required = {
            "add_member": {"email"},
            "extend_member": {"email", "months"},
            "remove_member": {"email", "reason"},
            "migrate_from_recipients": {"expires_at"},
            "bulk_add_members": {"emails"},
            "bulk_extend_members": {"months"},
            "bulk_set_expiry": {"expires_at"},
            "record_payment": {"email", "amount"},
            # set_expiry's `note` has a default at the MCP layer even though the service requires it.
            "set_expiry": {"email"},
        }
        for name, fields in required.items():
            with self.subTest(name):
                self.assertEqual(set(self.tools[name].input_schema["required"]), fields)
        for name in ("bulk_add_members", "bulk_extend_members", "bulk_set_expiry", "migrate_from_recipients"):
            with self.subTest(name):
                self.assertIs(self.tools[name].input_schema["properties"]["dry_run"]["default"], True)

    def test_instructions_document_the_safety_rules(self):
        for text in ("fails OPEN", "MEMBERSHIP_ENFORCEMENT=0", "confirm=true", "bulk_add_members", "dry_run=false"):
            with self.subTest(text):
                self.assertIn(text, mcp_server.INSTRUCTIONS)


class ResultEnvelopeTests(unittest.TestCase):
    """_call is what makes every tool's error path clean, parseable JSON with
    is_error set correctly, instead of the framework's default
    "Error executing tool <name>: <message>" prefix-polluted text.
    """

    def test_success_service_error_and_unexpected_error_envelopes(self):
        def service_error():
            raise ServiceError("member_not_found", "no such member", {"email": "a@example.com"})

        def unexpected():
            raise RuntimeError("something broke")

        ok = _call(lambda: {"hello": "world"})
        self.assertFalse(ok.is_error)
        self.assertEqual(payload(ok), {"hello": "world"})

        err = _call(service_error)
        self.assertTrue(err.is_error)
        self.assertEqual(payload(err), {"error": {"code": "member_not_found", "message": "no such member", "details": {"email": "a@example.com"}}})

        boom = _call(unexpected)
        self.assertTrue(boom.is_error)
        self.assertEqual(payload(boom)["error"]["code"], "internal_error")
        self.assertEqual(payload(boom)["error"]["message"], "something broke")

    def test_error_text_is_never_prefixed_by_the_framework(self):
        # Raising ToolError from a tool makes the framework prepend "Error executing tool <name>: ",
        # breaking naive json.loads(). _call must never raise; it returns CallToolResult directly.
        for result in (_call(lambda: (_ for _ in ()).throw(ServiceError("x", "y"))), _call(lambda: 1 / 0)):
            self.assertFalse(result.content[0].text.startswith("Error executing tool"))
            self.assertIn("error", payload(result))


class ToolWiringTests(unittest.TestCase):
    def test_every_tool_maps_its_params_to_the_service_call(self):
        m, o = mcp_server.members_svc, mcp_server.ops_svc
        # (tool, tool kwargs, service module, service function, expected positional args, expected kwargs)
        cases = [
            ("list_members", {"status": "expired", "expiring_within_days": 7}, m, "list_members", (), {"status": "expired", "expiring_within_days": 7}),
            ("get_member", {"email": "a@x.io"}, m, "get_member", ("a@x.io",), {}),
            (
                "add_member",
                {"email": "a@x.io", "months": 2, "note": "n", "joined_at": "2026-08-10", "amount": 99.5, "currency": "CNY",
                 "channel": "wechat", "paid_at": "2026-10-01", "ref": "T1"},
                m, "add_member", ("a@x.io", 2),
                {"note": "n", "joined_at": "2026-08-10", "expires_at": None, "align": False,
                 "payment": {"amount": 99.5, "currency": "CNY", "channel": "wechat", "paid_at": "2026-10-01", "ref": "T1"}},
            ),
            ("add_member", {"email": "a@x.io", "align": True}, m, "add_member", ("a@x.io", None), {"align": True, "payment": None}),
            ("extend_member", {"email": "a@x.io", "months": 2, "amount": 10}, m, "extend_member", ("a@x.io", 2), {"note": None, "payment": {"amount": 10}}),
            ("set_expiry", {"email": "a@x.io", "expires_at": "2026-12-09"}, m, "set_expiry", ("a@x.io", "2026-12-09", "manual correction via MCP"), {}),
            ("set_expiry", {"email": "a@x.io"}, m, "set_expiry", ("a@x.io", None, "manual correction via MCP"), {}),
            ("remove_member", {"email": "a@x.io", "reason": "refund"}, m, "remove_member", ("a@x.io", "refund"), {}),
            ("expiring_report", {}, m, "expiring_report", (), {"within_days": 14}),
            ("expiring_report", {"within_days": 30}, m, "expiring_report", (), {"within_days": 30}),
            ("sync_recipients", {}, m, "sync_recipients", (), {}),
            ("migrate_from_recipients", {"expires_at": "2026-10-09"}, m, "migrate_from_recipients", ("2026-10-09", None), {"dry_run": True}),
            ("migrate_from_recipients", {"expires_at": "2026-10-09", "note": "n", "dry_run": False}, m, "migrate_from_recipients",
             ("2026-10-09", "n"), {"dry_run": False}),
            ("bulk_add_members", {"emails": "a@x.io b@x.io", "expires_at": "2026-11-01"}, m, "bulk_add",
             ("a@x.io b@x.io",), {"expires_at": "2026-11-01", "months": None, "align": False, "payment": None, "dry_run": True}),
            ("bulk_add_members", {"emails": ["a@x.io"], "months": 1, "amount": 5, "dry_run": False}, m, "bulk_add",
             (["a@x.io"],), {"months": 1, "payment": {"amount": 5}, "dry_run": False}),
            ("bulk_extend_members", {"months": 1, "all_active": True}, m, "bulk_extend",
             (None,), {"months": 1, "all_active": True, "note": None, "dry_run": True}),
            ("bulk_set_expiry", {"expires_at": "2099-06-09", "emails": ["a@x.io"], "dry_run": False}, m, "bulk_set_expiry",
             (["a@x.io"],), {"expires_at": "2099-06-09", "all_active": False, "dry_run": False}),
            ("record_payment", {"email": "a@x.io", "amount": 5.5, "currency": "CNY", "channel": "alipay", "ref": "r", "note": "oct"}, m, "record_payment",
             ("a@x.io", 5.5), {"currency": "CNY", "channel": "alipay", "paid_at": None, "ref": "r", "note": "oct"}),
            ("payments_report", {"since": "2026-09-01", "until": "2026-10-01"}, m, "payments_report", (), {"since": "2026-09-01", "until": "2026-10-01"}),
            ("ops_status", {}, o, "status", (), {}),
            ("ops_diagnose", {}, o, "diagnose", (), {}),
            ("ops_run_job", {"kind": "picks", "force": True, "timeout": 30, "confirm": True}, o, "run_job", ("picks",),
             {"force": True, "timeout": 30, "confirm": True}),
            ("ops_run_job", {"kind": "test_email"}, o, "run_job", ("test_email",), {"force": False, "timeout": None, "confirm": False}),
            ("ops_logs", {"name": "scheduler"}, o, "logs", ("scheduler",), {"lines": 100, "grep": None}),
            ("ops_logs", {"name": "email", "lines": 5, "grep": "x"}, o, "logs", ("email",), {"lines": 5, "grep": "x"}),
            ("ops_recent_deliveries", {"limit": 7}, o, "recent_deliveries", (), {"limit": 7}),
            ("ops_schedule_preview", {}, o, "schedule_preview", (), {"days": 3}),
            ("ops_historical_resend_preview", {"weekly_date": "Updated on Aug 7, 2026"}, o, "historical_resend_preview", ("Updated on Aug 7, 2026",), {}),
        ]
        for tool, params, module, name, args, kwargs in cases:
            with self.subTest(tool=tool, params=params):
                with patch.object(module, name, return_value=STUB) as service:
                    result = getattr(mcp_server, tool)(**params)
                self.assertFalse(result.is_error)
                self.assertEqual(payload(result), STUB)
                service.assert_called_once()
                call = service.call_args
                self.assertEqual(call.args, args)
                self.assertEqual({k: call.kwargs[k] for k in kwargs}, kwargs)

    def test_route_preview_delegates_and_rejects_an_unknown_route_cleanly(self):
        fake_result = {"route": "admin", "included": ["admin@example.com"], "excluded": [], "enforcement": True, "counts": {}}
        with patch("quantcheck.mcp_server._route_preview", return_value=fake_result) as mocked:
            result = mcp_server.route_preview(route="admin")
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result), fake_result)
        self.assertEqual(mocked.call_args.args[0].value, "admin")

        bad = mcp_server.route_preview(route="not_a_real_route")
        self.assertTrue(bad.is_error)
        self.assertEqual(payload(bad)["error"]["code"], "invalid_route")

    def test_tool_is_callable_through_the_server_and_returns_json(self):
        with patch.object(mcp_server.members_svc, "list_members", return_value={"count": 0, "members": []}):
            result = asyncio.run(server.call_tool("list_members", {}))
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result), {"count": 0, "members": []})


class DefaultsAgainstATmpRootTests(unittest.TestCase):
    """service.members/service.ops resolve their root via quantcheck.config.get_root(), which reads
    os.environ on every call, so patching QUANTCHECK_HOME per test isolates the tools from the real
    install without a root= parameter on the MCP tools.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(f"QUANTCHECK_HOME={self.root}\n", encoding="utf-8")
        (self.root / "notify_recipients.txt").write_text("a@example.com\n", encoding="utf-8")
        env_patch = patch.dict("os.environ", {"QUANTCHECK_HOME": str(self.root)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_bulk_tools_and_migrate_default_to_dry_run_and_write_nothing(self):
        before = self.snapshot()
        calls = {
            "migrate_from_recipients": {"expires_at": "2026-10-09"},
            "bulk_add_members": {"emails": "n1@example.com n2@example.com", "expires_at": "2026-11-01"},
            "bulk_extend_members": {"months": 1, "emails": "a@example.com"},
            "bulk_set_expiry": {"expires_at": "2026-11-01", "emails": "a@example.com"},
        }
        for tool, params in calls.items():
            with self.subTest(tool):
                result = getattr(mcp_server, tool)(**params)
                self.assertTrue(payload(result)["dry_run"])
                self.assertEqual(self.snapshot(), before)  # byte-identical: no store, no .bak, recipients untouched

        applied = mcp_server.bulk_add_members(emails="n1@example.com", expires_at="2026-11-01", dry_run=False)
        self.assertEqual(payload(applied)["counts"]["ok"], 1)
        self.assertTrue((self.root / "state" / "memberships.json").exists())

    def test_ops_run_job_confirm_gate_is_enforced_through_the_tool(self):
        for params in ({"kind": "test_email"}, {"kind": "picks", "force": True}):
            with self.subTest(params), patch("quantcheck.service.ops.scheduler_mod.run_cmd") as run_cmd:
                result = mcp_server.ops_run_job(**params)
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result)["error"]["code"], "confirmation_required")
                run_cmd.assert_not_called()

    def test_real_service_results_and_errors_are_clean_json(self):
        listed = mcp_server.list_members()
        self.assertFalse(listed.is_error)
        self.assertEqual(payload(listed)["count"], 0)

        missing = mcp_server.get_member(email="nobody@example.com")
        self.assertTrue(missing.is_error)
        self.assertEqual(payload(missing)["error"]["code"], "member_not_found")

        bad = mcp_server.add_member(email="c@example.com")  # no expiry rule given
        self.assertTrue(bad.is_error)
        self.assertEqual(payload(bad)["error"]["code"], "invalid_arguments")

        diagnose = mcp_server.ops_diagnose()
        self.assertFalse(diagnose.is_error)
        self.assertIn("findings", payload(diagnose))


if __name__ == "__main__":
    unittest.main()
