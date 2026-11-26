import json
import unittest
from http.client import HTTPConnection
from threading import Thread
from urllib.parse import urlencode

from backend import ApiError, FamilyBackend, FamilyBackendServer, build_sample_backend
from partner_feed import PartnerFeed


def sample_register():
    with open("samples/register.json", encoding="utf-8") as handle:
        return json.load(handle)


class SampleDomainTest(unittest.TestCase):
    def setUp(self):
        self.backend = build_sample_backend()

    def test_sample_explains_clear_old_usage_and_holds_crossing_usage(self):
        view = self.backend.member_charge_basis("m-2", "1")
        self.assertEqual(view["totals"]["reported_units"], 15)
        self.assertEqual(view["totals"]["net_deducted_units"], 3)
        self.assertEqual(view["totals"]["held_unverified_units"], 12)
        self.assertTrue(view["period_status"]["signed"])

        by_id = {row["id"]: row for row in view["usage_records"]}
        crossing = by_id["m-2-13-17"]
        self.assertEqual(crossing["holds"][0]["reason"],
                         "cross_migration_without_quantitative_split")
        self.assertEqual(crossing["allocations"], [])

        clear = by_id["m-2-13-14"]
        allocation = clear["allocations"][0]
        self.assertEqual(allocation["context_group_id"], "g-a")
        self.assertEqual(allocation["units"], 3)
        self.assertEqual(allocation["fund_id"], "promise:arrangement:g-a:m-2")

    def test_bilateral_migration_capacity_and_commitment_differences(self):
        ga = self.backend.group_admin_view("g-a")
        gb = self.backend.group_admin_view("g-b")
        outgoing = next(row for row in ga["migrations"] if row["direction"] == "outgoing")
        incoming = next(row for row in gb["migrations"] if row["direction"] == "incoming")

        self.assertEqual(outgoing["status"], "effective")
        self.assertTrue(outgoing["both_group_consented"])
        self.assertEqual(outgoing["effective_date"], 15)
        self.assertEqual(outgoing["own_promised_units"], 20)
        self.assertEqual(outgoing["own_settled_units"], 3)
        self.assertEqual(outgoing["own_commitment_difference_units"], 17)
        self.assertEqual(outgoing["pending_verification_units"], 12)

        self.assertEqual(incoming["own_promised_units"], 9)
        self.assertEqual(incoming["own_commitment_difference_units"], 9)
        self.assertEqual(gb["pool"]["available_for_commitment_units"], 0)

    def test_new_group_promise_must_fit_available_pool(self):
        backend = FamilyBackend(sample_register())
        mig = backend.request_migration("m-2", "g-a", "g-b", 15)
        backend.accept_migration(mig["id"], "g-a")
        with self.assertRaises(ApiError) as raised:
            backend.accept_migration(mig["id"], "g-b", promised_units=10)
        self.assertEqual(raised.exception.code, "new_group_capacity_exceeded")
        self.assertEqual(raised.exception.details["available_units"], 9)

        view = backend.accept_migration(mig["id"], "g-b", promised_units=9)
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(backend.available_for_commitment("g-b"), 0)

    def test_single_consent_does_not_plan_double_sided_benefits(self):
        backend = FamilyBackend(sample_register())
        mig = backend.request_migration("m-2", "g-a", "g-b", 15)
        backend.accept_migration(mig["id"], "g-a")
        fresh = backend.migration_view(mig["id"])
        self.assertEqual(fresh["status"], "partially_accepted")
        self.assertFalse(fresh["membership_planned"])
        self.assertEqual(backend._group_at("m-2", 16), "g-a")

    def test_personal_pack_is_portable_but_original_group_pack_is_not(self):
        view = self.backend.member_charge_basis("m-2", "1")
        funds = {row["fund_id"]: row for row in view["entitlements"]}
        self.assertTrue(funds["personal-4"]["portable"])
        self.assertTrue(funds["personal-4"]["usable_at_as_of"])
        self.assertFalse(funds["shared-7"]["portable"])
        self.assertFalse(funds["shared-7"]["usable_at_as_of"])
        self.assertIn("不随迁组默认转移", funds["shared-7"]["ineligibility_reason_zh"])

        # 先用清新组承诺 9，下一笔新组区间用量应落到成员自购包。
        self.backend.report_usage("m-2", [16, 17], 9)
        self.backend.report_usage("m-2", [18, 19], 2)
        personal = self.backend.funds["personal-4"]
        new_promise = self.backend.funds["promise:commit:mig:m-2:g-a:g-b:15"]
        self.assertEqual(new_promise["used"], 9)
        self.assertEqual(personal["used"], 2)

    def test_other_members_continue_independently(self):
        self.backend.report_usage("m-1", [11, 12], 5)
        self.backend.report_usage("m-3", [11, 12], 4)
        m1 = self.backend.member_charge_basis("m-1", "1")
        m3 = self.backend.member_charge_basis("m-3", "1")
        self.assertEqual(m1["totals"]["net_deducted_units"], 5)
        self.assertEqual(m3["totals"]["net_deducted_units"], 4)
        self.assertEqual(m1["allocations"][0]["context_group_id"], "g-a")
        self.assertIn(m1["allocations"][0]["fund_id"], {"shared-7", "base:g-a"})
        self.assertEqual(m3["allocations"][0]["fund_id"],
                         "promise:arrangement:g-b:m-3")
        admin = self.backend.group_admin_view("g-a")
        member_rows = {row["member_id"]: row for row in admin["members"]}
        self.assertTrue(member_rows["m-1"]["service_continuity"])

    def test_late_usage_after_signing_creates_delta_without_rewriting_snapshot(self):
        before = self.backend.period_view("1")["statement"]
        self.assertEqual(before["allocated_units"], 3)
        self.backend.report_usage("m-2", [20, 21], 2, received_at=35)
        after = self.backend.period_view("1")
        self.assertEqual(after["statement"]["allocated_units"], 3)
        self.assertEqual(len(after["corrections"]), 1)
        correction = after["corrections"][0]
        self.assertEqual(correction["type"], "late_usage_difference")
        self.assertEqual(correction["delta_units"], 2)
        view = self.backend.member_charge_basis("m-2", "1")
        self.assertEqual(view["totals"]["net_deducted_units"], 5)

    def test_verified_split_after_signing_uses_difference_records(self):
        split = [
            {"window": [13, 15], "units": 4, "group_id": "g-a"},
            {"window": [15, 17], "units": 8, "group_id": "g-b"},
        ]
        self.backend.attach_usage_evidence("m-2-13-17", {"split": split})
        period = self.backend.period_view("1")
        self.assertEqual(period["statement"]["allocated_units"], 3)
        self.assertEqual(len(period["corrections"]), 1)
        correction = period["corrections"][0]
        self.assertEqual(correction["type"], "post_signing_difference")
        self.assertEqual(sum(correction["delta_by_fund"].values()), 12)

        view = self.backend.member_charge_basis("m-2", "1")
        self.assertEqual(view["totals"]["held_unverified_units"], 0)
        self.assertEqual(view["totals"]["net_deducted_units"], 15)


    def test_member_caps_are_registered_and_enforced(self):
        backend = FamilyBackend()
        backend.register_group(
            "g-cap", [1, 20], 10, ["m-cap"], member_caps={"m-cap": 2}
        )
        backend.report_usage("m-cap", [5, 6], 3)
        cap = backend.member_cap_view("g-cap", "m-cap")
        usage_id = next(iter(backend.usage))
        usage = backend.usage_view(usage_id)
        self.assertEqual(cap["used_units"], 2)
        self.assertFalse(cap["exceeded"])
        self.assertEqual(usage["charge_lines"][0]["reason"], "member_cap_limit_reached")
        self.assertEqual(usage["charge_lines"][0]["charge_units"], 0)

    def test_group_admin_can_start_next_migration(self):
        backend = build_sample_backend()
        second = backend.request_migration(
            "m-2", "g-b", "g-a", 20, actor_id="admin-g-b"
        )
        self.assertEqual(second["status"], "awaiting_consents")
        backend.accept_migration(second["id"], "g-b", actor_id="admin-g-b")
        backend.accept_migration(second["id"], "g-a", actor_id="admin-g-a")
        effective = backend.activate_migration(second["id"], at=20)
        self.assertEqual(effective["status"], "effective")
        self.assertEqual(backend._group_at("m-2", 21), "g-a")


class AuthorizationHttpTest(unittest.TestCase):
    def setUp(self):
        self.backend = build_sample_backend()
        self.server = FamilyBackendServer(0, self.backend)
        self.port = self.server.server_port
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def get(self, path, actor=None):
        headers = {}
        if actor:
            headers["X-Actor-Id"] = actor
        conn = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def post(self, path, payload, actor=None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        conn = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request("POST", path, body=json.dumps(payload), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_admin_view_redacts_communication_detail(self):
        status, body = self.get("/v1/groups/g-a/admin-view", "admin-g-a")
        self.assertEqual(status, 200)
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("window", raw)
        self.assertNotIn("m-2-13-17", raw)

        status, _ = self.get("/v1/members/m-2/charge-basis?period=1", "admin-g-a")
        self.assertEqual(status, 403)

    def test_support_requires_member_authorization(self):
        status, body = self.get("/v1/support/members/m-2/current", "support-1")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "authorization_required")

        status, grant = self.post("/v1/authorizations", {
            "member_id": "m-2", "agent_id": "support-1", "period_id": "1"
        }, "m-2")
        self.assertEqual(status, 201)
        self.assertTrue(grant["id"])

        status, body = self.get("/v1/support/members/m-2/current", "support-1")
        self.assertEqual(status, 200)
        self.assertIn("usage_records", body)

    def test_member_can_read_own_charge_basis(self):
        status, body = self.get("/v1/members/m-2/charge-basis?period=1", "m-2")
        self.assertEqual(status, 200)
        self.assertEqual(body["member_id"], "m-2")


class PartnerSyncTest(unittest.TestCase):
    def setUp(self):
        self.feed = PartnerFeed(0)
        self.feed_port = self.feed.server_port
        self.addCleanup(self.feed.server_close)
        self.feed_thread = Thread(target=self.feed.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        self.feed_thread.start()
        self.addCleanup(self.feed_thread.join, 2)
        self.addCleanup(self.feed.shutdown)

    def test_sync_intake_and_delayed_receipt(self):
        backend = FamilyBackend()
        result = backend.sync_partner_feed(f"http://127.0.0.1:{self.feed_port}")
        self.assertEqual(result["sequences"], [1, 2, 3, 4, 5])
        receipt_statuses = {row["ref"]: row["status"] for row in result["receipts"]}
        self.assertEqual(receipt_statuses["m-2-13-17"], "applied_after_retry")
        view = backend.member_charge_basis("m-2", "1")
        self.assertEqual(view["totals"]["held_unverified_units"], 12)
        self.assertTrue(view["period_status"]["locked"])


if __name__ == "__main__":
    unittest.main()
