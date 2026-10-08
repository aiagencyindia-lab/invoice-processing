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
ORGS = ["Zentrix Cloudworks"]


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
        assert ap.build_chain(
            {**ACME, "_routing": "REVIEW"}, [], organization_names=ORGS
        ) == [ap.ROLE_FALLBACK]

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
        chain = ap.build_chain(ACME, rules, organization_names=ORGS)
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
        assert [
            s.source_rule_id
            for s in ap.build_chain(ACME, rules, organization_names=ORGS)
        ] == ["high"]

    def test_duplicate_assignee_collapses_to_first_position(self):
        rules = [
            rule(
                "vendor", {"vendor_name": ACME["vendor_name"]}, [CFO], rid="ven"
            ),
            rule("amount", {"min_amount": 1}, [FIN, CFO], rid="amt"),
        ]
        assert [
            s.assignee.value
            for s in ap.build_chain(ACME, rules, organization_names=ORGS)
        ] == ["cfo@a.test", "Finance"]

    def test_entity_match_folds_like_step_1_2(self):
        rules = [
            rule("entity", {"organization_name": "ZENTRIX  CLOUD-WORKS"}, [FIN])
        ]
        assert (
            ap.build_chain(ACME, rules, organization_names=ORGS)[
                0
            ].assignee.value
            == "Finance"
        )

    def test_review_invoice_with_no_match_gets_role_fallback_only(self):
        assert ap.build_chain(
            {**ACME, "_routing": "REVIEW"},
            [rule("vendor", {"vendor_name": "Other"}, [IT])],
            organization_names=ORGS,
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
