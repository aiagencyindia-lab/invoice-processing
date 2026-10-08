"""Who must approve an invoice, and whether this person may. Pure: inputs in,
answer out, so the rules can be read and tested without a database."""

from typing import ClassVar

from invoice_processing import approvals as ap

ACME = {
    "vendor_name": "Cloudhaven Infra Services Pvt Ltd",
    "customer_name": "Zentrix Cloudworks Private Limited",
    "invoice_total_inc_tax": 120000.0,
    "currency": "INR",
    "_routing": "AUTO",
}


def rule(kind, match, steps, rid="r1", enabled=True, created_at="2026-01-01"):
    return {
        "id": rid,
        "name": rid,
        "kind": kind,
        "match": match,
        "steps": steps,
        "enabled": enabled,
        "created_at": created_at,
    }


FIN = {"kind": "group", "value": "Finance"}
CFO = {"kind": "user", "value": "cfo@a.test"}
IT = {"kind": "user", "value": "it@a.test"}


class TestNeedsPerson:
    def test_review_routing_always_needs_a_person(self):
        assert ap.needs_person({**ACME, "_routing": "REVIEW"}, [])

    def test_auto_below_every_tier_is_touchless(self):
        assert not ap.needs_person(
            ACME, [rule("amount", {"min_amount": 500000}, [FIN])]
        )

    def test_auto_at_or_above_a_tier_needs_a_person(self):
        assert ap.needs_person(
            ACME, [rule("amount", {"min_amount": 120000}, [FIN])]
        )

    def test_a_tier_in_another_currency_does_not_count(self):
        assert not ap.needs_person(
            ACME, [rule("amount", {"min_amount": 1, "currency": "AUD"}, [FIN])]
        )

    def test_disabled_rules_do_not_count(self):
        assert not ap.needs_person(
            ACME, [rule("amount", {"min_amount": 1}, [FIN], enabled=False)]
        )


class TestChain:
    def test_no_rule_means_any_approver(self):
        assert ap.build_chain({**ACME, "_routing": "REVIEW"}, []) == [
            ap.ROLE_FALLBACK
        ]

    def test_fixed_order_vendor_entity_amount(self):
        rules = [
            rule("amount", {"min_amount": 100000}, [FIN, CFO], rid="amt"),
            rule(
                "entity",
                {"organization_name": "Zentrix Cloudworks"},
                [{"kind": "group", "value": "Zentrix approvers"}],
                rid="ent",
            ),
            rule(
                "vendor",
                {"vendor_name": "Cloudhaven Infra Services Private Limited"},
                [IT],
                rid="ven",
            ),
        ]
        chain = ap.build_chain(ACME, rules)
        assert [(s.assignee.value, s.source_rule_id) for s in chain] == [
            ("it@a.test", "ven"),
            ("Zentrix approvers", "ent"),
            ("Finance", "amt"),
            ("cfo@a.test", "amt"),
        ]

    def test_only_the_highest_matching_tier_applies(self):
        rules = [
            rule("amount", {"min_amount": 50000}, [FIN], rid="low"),
            rule("amount", {"min_amount": 100000}, [CFO], rid="high"),
            rule("amount", {"min_amount": 500000}, [IT], rid="top"),
        ]
        assert [s.source_rule_id for s in ap.build_chain(ACME, rules)] == [
            "high"
        ]

    def test_duplicate_assignee_collapses_to_first_position(self):
        rules = [
            rule(
                "vendor", {"vendor_name": ACME["vendor_name"]}, [CFO], rid="ven"
            ),
            rule("amount", {"min_amount": 1}, [FIN, CFO], rid="amt"),
        ]
        assert [s.assignee.value for s in ap.build_chain(ACME, rules)] == [
            "cfo@a.test",
            "Finance",
        ]

    def test_entity_match_folds_like_step_1_2(self):
        rules = [
            rule("entity", {"organization_name": "ZENTRIX  CLOUD-WORKS"}, [FIN])
        ]
        assert ap.build_chain(ACME, rules)[0].assignee.value == "Finance"

    def test_review_invoice_with_no_match_gets_role_fallback_only(self):
        assert ap.build_chain(
            {**ACME, "_routing": "REVIEW"},
            [rule("vendor", {"vendor_name": "Other"}, [IT])],
        ) == [ap.ROLE_FALLBACK]


class TestSatisfaction:
    GROUPS: ClassVar[dict[str, list[str]]] = {
        "Finance": ["fin1@a.test", "Fin2@A.test"]
    }

    def test_named_user(self):
        assert (
            ap.satisfaction(
                "cfo@a.test",
                "approver",
                {
                    "assignee_kind": "user",
                    "assignee_value": "CFO@a.test",
                    "escalated_to": None,
                },
                groups=self.GROUPS,
                delegators_of=[],
            )
            == "assigned to you"
        )

    def test_group_member_case_insensitive(self):
        assert (
            ap.satisfaction(
                "fin2@a.test",
                "clerk",
                {
                    "assignee_kind": "group",
                    "assignee_value": "Finance",
                    "escalated_to": None,
                },
                groups=self.GROUPS,
                delegators_of=[],
            )
            == "member of Finance"
        )

    def test_role_holder(self):
        step = {
            "assignee_kind": "role",
            "assignee_value": "approver",
            "escalated_to": None,
        }
        assert (
            ap.satisfaction(
                "x@a.test", "admin", step, groups={}, delegators_of=[]
            )
            == "has the approver role"
        )
        assert (
            ap.satisfaction(
                "x@a.test", "clerk", step, groups={}, delegators_of=[]
            )
            is None
        )

    def test_active_delegate_of_the_assignee(self):
        step = {
            "assignee_kind": "user",
            "assignee_value": "cfo@a.test",
            "escalated_to": None,
        }
        assert (
            ap.satisfaction(
                "pa@a.test",
                "clerk",
                step,
                groups={},
                delegators_of=["cfo@a.test"],
            )
            == "delegate for cfo@a.test"
        )

    def test_escalation_target_user_and_role(self):
        step = {
            "assignee_kind": "user",
            "assignee_value": "cfo@a.test",
            "escalated_to": "boss@a.test",
        }
        assert (
            ap.satisfaction(
                "boss@a.test", "clerk", step, groups={}, delegators_of=[]
            )
            == "escalated to you"
        )
        step = {**step, "escalated_to": "role:admin"}
        assert (
            ap.satisfaction(
                "root@a.test", "admin", step, groups={}, delegators_of=[]
            )
            == "escalated to admins"
        )

    def test_nobody_else(self):
        assert (
            ap.satisfaction(
                "x@a.test",
                "approver",
                {
                    "assignee_kind": "user",
                    "assignee_value": "cfo@a.test",
                    "escalated_to": None,
                },
                groups={},
                delegators_of=[],
            )
            is None
        )


class TestSegregation:
    def test_uploader_is_refused(self):
        assert "uploaded" in ap.refusal(
            "clerk@a.test", uploader="Clerk@a.test", previous_deciders=[]
        )

    def test_previous_decider_is_refused(self):
        assert "earlier step" in ap.refusal(
            "a@a.test", uploader=None, previous_deciders=["a@a.test"]
        )

    def test_otherwise_allowed(self):
        assert (
            ap.refusal(
                "b@a.test", uploader="a@a.test", previous_deciders=["a@a.test"]
            )
            is None
        )

    def test_editor_as_sole_approver_needs_a_second(self):
        assert ap.editor_needs_second(
            "ed@a.test", ["ed@a.test"], is_only_pending=True
        )
        assert not ap.editor_needs_second(
            "ed@a.test", ["ed@a.test"], is_only_pending=False
        )
        assert not ap.editor_needs_second(
            "other@a.test", ["ed@a.test"], is_only_pending=True
        )


class TestMalformedAndEdges:
    GOOD = rule("amount", {"min_amount": 1}, [FIN], rid="good")

    def chain(self, bad, invoice=ACME):
        return ap.build_chain(invoice, [bad, self.GOOD])

    def test_bad_min_amount_is_skipped(self):
        bad = rule("amount", {"min_amount": "abc"}, [CFO], rid="bad")
        assert [s.source_rule_id for s in self.chain(bad)] == ["good"]
        assert ap.needs_person(ACME, [bad]) is False

    def test_match_none_is_skipped(self):
        bad = rule("vendor", None, [CFO], rid="bad")
        assert [s.source_rule_id for s in self.chain(bad)] == ["good"]

    def test_missing_id_is_skipped(self):
        bad = rule("amount", {"min_amount": 1}, [CFO])
        del bad["id"]
        assert [s.source_rule_id for s in self.chain(bad)] == ["good"]

    def test_step_missing_value_is_skipped(self):
        bad = rule("amount", {"min_amount": 50}, [{"kind": "user"}], rid="bad")
        assert [s.source_rule_id for s in self.chain(bad)] == ["good"]

    def test_non_numeric_total_matches_no_tier(self):
        inv = {**ACME, "invoice_total_inc_tax": "n/a"}
        assert not ap.needs_person(inv, [self.GOOD])
        assert ap.build_chain({**inv, "_routing": "REVIEW"}, [self.GOOD]) == [
            ap.ROLE_FALLBACK
        ]

    def test_none_total_on_review_gets_fallback(self):
        inv = {**ACME, "invoice_total_inc_tax": None, "_routing": "REVIEW"}
        assert ap.build_chain(inv, [self.GOOD]) == [ap.ROLE_FALLBACK]

    def test_equal_floor_earliest_created_wins(self):
        a = rule(
            "amount",
            {"min_amount": 10},
            [FIN],
            rid="late",
            created_at="2026-02-01",
        )
        b = rule(
            "amount",
            {"min_amount": 10},
            [CFO],
            rid="early",
            created_at="2026-01-01",
        )
        assert [s.source_rule_id for s in ap.build_chain(ACME, [a, b])] == [
            "early"
        ]

    def test_equal_floor_same_time_input_order(self):
        a = rule("amount", {"min_amount": 10}, [FIN], rid="first")
        b = rule("amount", {"min_amount": 10}, [CFO], rid="second")
        assert [s.source_rule_id for s in ap.build_chain(ACME, [a, b])] == [
            "first"
        ]

    def test_disabled_vendor_rule_ignored(self):
        r = rule(
            "vendor", {"vendor_name": ACME["vendor_name"]}, [IT], enabled=False
        )
        assert ap.build_chain({**ACME, "_routing": "REVIEW"}, [r]) == [
            ap.ROLE_FALLBACK
        ]

    def test_satisfaction_edges(self):
        grp = {
            "assignee_kind": "group",
            "assignee_value": "Nope",
            "escalated_to": None,
        }
        assert (
            ap.satisfaction(
                "x@a.test", "admin", grp, groups={}, delegators_of=[]
            )
            is None
        )
        role = {
            "assignee_kind": "role",
            "assignee_value": "superuser",
            "escalated_to": None,
        }
        assert (
            ap.satisfaction(
                "x@a.test", "admin", role, groups={}, delegators_of=[]
            )
            is None
        )
        esc = {
            "assignee_kind": "user",
            "assignee_value": "c@a.test",
            "escalated_to": 5,
        }
        assert (
            ap.satisfaction(
                "x@a.test", "clerk", esc, groups={}, delegators_of=[]
            )
            is None
        )
