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
            == "escalated from cfo@a.test"
        )
        step = {**step, "escalated_to": "role:admin"}
        assert (
            ap.satisfaction(
                "root@a.test", "admin", step, groups={}, delegators_of=[]
            )
            == "escalated from cfo@a.test"
        )
        group = {
            "assignee_kind": "group",
            "assignee_value": "Finance",
            "escalated_to": "role:admin",
        }
        assert (
            ap.satisfaction(
                "root@a.test", "admin", group, groups={}, delegators_of=[]
            )
            == "escalated from Finance"
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

    def test_non_numeric_total_takes_the_highest_tier(self):
        inv = {**ACME, "invoice_total_inc_tax": "n/a"}
        assert ap.needs_person(inv, [self.GOOD])
        assert [s.source_rule_id for s in ap.build_chain(inv, [self.GOOD])] == [
            "good"
        ]

    def test_none_total_on_review_takes_the_highest_tier(self):
        inv = {**ACME, "invoice_total_inc_tax": None, "_routing": "REVIEW"}
        assert [s.source_rule_id for s in ap.build_chain(inv, [self.GOOD])] == [
            "good"
        ]

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


class TestUnreadableTotal:
    """A total the engine cannot read must not slip under every tier: the
    highest tier applies, so the invoice reaches the most senior approver."""

    LOW = rule("amount", {"min_amount": 0}, [FIN], rid="low")
    HIGH = rule("amount", {"min_amount": 100000}, [CFO], rid="high")
    OTHER_CCY = rule(
        "amount", {"min_amount": 900000, "currency": "USD"}, [IT], rid="usd"
    )

    def tier(self, total, rules=None):
        inv = {**ACME, "invoice_total_inc_tax": total}
        rules = rules or [self.LOW, self.HIGH, self.OTHER_CCY]
        return ap.needs_person(inv, rules), [
            s.source_rule_id for s in ap.build_chain(inv, rules)
        ]

    def test_thousands_separator_takes_the_highest_tier(self):
        assert self.tier("12,500.00") == (True, ["high"])

    def test_missing_total_takes_the_highest_tier(self):
        assert self.tier(None) == (True, ["high"])

    def test_nan_takes_the_highest_tier(self):
        assert self.tier("nan") == (True, ["high"])
        assert self.tier(float("inf")) == (True, ["high"])

    def test_invoice_without_currency_may_take_any_tier(self):
        inv = {**ACME, "invoice_total_inc_tax": None, "currency": None}
        rules = [self.LOW, self.HIGH, self.OTHER_CCY]
        assert [s.source_rule_id for s in ap.build_chain(inv, rules)] == ["usd"]

    def test_no_amount_rule_leaves_auto_touchless(self):
        assert (
            self.tier(None, [rule("vendor", {"vendor_name": "Z"}, [IT])])[0]
            is False
        )


class TestCallerBeforeDelegators:
    def test_own_membership_never_reads_as_a_delegation(self):
        step = {
            "assignee_kind": "group",
            "assignee_value": "Finance",
            "escalated_to": None,
        }
        groups = {
            "Finance": [f"d{i}@a.test" for i in range(20)] + ["me@a.test"]
        }
        assert (
            ap.satisfaction(
                "me@a.test",
                "clerk",
                step,
                groups=groups,
                delegators_of=[f"d{i}@a.test" for i in range(20)],
            )
            == "member of Finance"
        )


class TestGlRules:
    GL: ClassVar[dict] = {**ACME, "_gl_accounts": ["6100-IT", None, "4000"]}

    def ids(self, invoice, rules):
        return [s.source_rule_id for s in ap.build_chain(invoice, rules)]

    def test_matches_a_casefolded_prefix_of_any_account(self):
        r = rule("gl", {"gl_prefix": "6100-it"}, [IT], rid="gl")
        assert self.ids(self.GL, [r]) == ["gl"]

    def test_no_matching_account_falls_back(self):
        r = rule("gl", {"gl_prefix": "7"}, [IT], rid="gl")
        assert ap.build_chain(self.GL, [r]) == [ap.ROLE_FALLBACK]

    def test_missing_or_garbage_accounts_never_match(self):
        r = rule("gl", {"gl_prefix": "6"}, [IT], rid="gl")
        for junk in (None, "6100", 6100, [6100, {"a": 1}]):
            invoice = {**ACME, "_gl_accounts": junk}
            assert ap.build_chain(invoice, [r]) == [ap.ROLE_FALLBACK]
        assert ap.build_chain(ACME, [r]) == [ap.ROLE_FALLBACK]

    def test_blank_or_missing_prefix_is_skipped(self):
        for match in (
            {},
            {"gl_prefix": ""},
            {"gl_prefix": " "},
            {"gl_prefix": 6},
        ):
            bad = rule("gl", match, [IT], rid="bad")
            assert ap.build_chain(self.GL, [bad]) == [ap.ROLE_FALLBACK]

    def test_order_is_vendor_entity_gl_amount(self):
        rules = [
            rule("amount", {"min_amount": 1}, [CFO], rid="amt"),
            rule("gl", {"gl_prefix": "4"}, [FIN], rid="gl"),
            rule(
                "entity",
                {"organization_name": "Zentrix"},
                [{"kind": "user", "value": "ent@a.test"}],
                rid="ent",
            ),
            rule(
                "vendor", {"vendor_name": ACME["vendor_name"]}, [IT], rid="ven"
            ),
        ]
        assert self.ids(self.GL, rules) == ["ven", "ent", "gl", "amt"]

    def test_gl_rule_does_not_pull_a_touchless_invoice_to_a_person(self):
        r = rule("gl", {"gl_prefix": "6"}, [IT], rid="gl")
        assert not ap.needs_person(self.GL, [r])


class TestControllerSeat:
    """A controller step is finance sign-off: only a controller decides it,
    whatever the ladder says and however late it is."""

    STEP: ClassVar[dict] = {
        "assignee_kind": "role",
        "assignee_value": "controller",
    }

    def test_only_a_controller_fills_a_controller_seat(self):
        assert ap.fills("controller", "controller")
        assert not ap.fills("admin", "controller")
        assert not ap.fills("approver", "controller")
        assert ap.fills("controller", "approver")
        assert ap.fills("admin", "approver")
        assert not ap.fills("clerk", "approver")
        assert not ap.fills("nobody", "approver")

    def _why(self, role, step):
        return ap.satisfaction(
            "x@a.test", role, step, groups={}, delegators_of=[]
        )

    def test_a_controller_step_is_decided_by_a_controller_only(self):
        assert self._why("controller", self.STEP) == "has the controller role"
        assert self._why("admin", self.STEP) is None
        assert self._why("approver", self.STEP) is None

    def test_an_escalated_controller_step_stays_controller_only(self):
        step = {**self.STEP, "escalated_to": "role:admin"}
        assert self._why("admin", step) is None
        assert self._why("controller", step) == "has the controller role"
