"""The report as a print-styled page. No ids: a person reads this."""

from __future__ import annotations

from ..html import kv, page, section, table


def report_html(report: dict) -> str:
    audit, s = report["audit"], report["summary"]
    cur = s["currency"] or ""
    body = section(
        "Executive summary",
        kv(
            [
                (
                    "Period",
                    f"{audit.get('period_from') or '…'} to {audit.get('period_to') or '…'}",
                ),
                ("Approver", audit.get("approver")),
                ("Findings", s["findings"]),
                ("Detected impact", f"{s['detected_impact']} {cur}"),
                ("Accepted impact", f"{s['accepted_impact']} {cur}"),
                ("Recovered", f"{s['recovered']} {cur}"),
                ("Prevented", f"{s['prevented']} {cur}"),
                ("Written off", f"{s['written_off']} {cur}"),
                ("At risk in open holds", f"{s['at_risk_holds']} {cur}"),
                ("Generated", report["generated_at"]),
            ]
        ),
    )
    cols = ["Count", "Impact", "Recovered", "Prevented"]
    body += section(
        "By type",
        table(
            ["Type", "Engine", *cols],
            [
                [
                    r["type"],
                    r["engine"],
                    r["count"],
                    r["impact"],
                    r["recovered"],
                    r["prevented"],
                ]
                for r in report["by_type"]
            ],
        ),
    )
    body += section(
        "By vendor",
        table(
            ["Vendor", *cols],
            [
                [
                    r["vendor"] or "(unknown)",
                    r["count"],
                    r["impact"],
                    r["recovered"],
                    r["prevented"],
                ]
                for r in report["by_vendor"]
            ],
        ),
    )
    body += section(
        "Top recoveries",
        table(
            ["Type", "Vendor", "Status", "Impact", "Summary", "Evidence"],
            [
                [
                    t["type"],
                    t["vendor"],
                    t["status"],
                    t["impact"],
                    t["summary"],
                    "; ".join(
                        f"{c['label']}: {c['excerpt'] or ''}"
                        for c in t["citations"]
                    ),
                ]
                for t in report["top_recoveries"]
            ],
        ),
    )
    body += section(
        "Prevention",
        table(
            [
                "Engine",
                "Findings",
                "Impact",
                "Mode now",
                "Suggested",
                "Thresholds",
            ],
            [
                [
                    p["engine"],
                    p["count"],
                    p["impact"],
                    p["mode_now"],
                    p["mode_suggested"],
                    ", ".join(p["thresholds_hint"]),
                ]
                for p in report["prevention"]
            ],
        ),
    )
    itc = report["itc_exposure"]
    body += section(
        "ITC exposure",
        kv([("Findings", itc["count"]), ("Impact", f"{itc['impact']} {cur}")])
        + table(
            ["Vendor", *cols],
            [
                [
                    r["vendor"] or "(unknown)",
                    r["count"],
                    r["impact"],
                    r["recovered"],
                    r["prevented"],
                ]
                for r in itc["by_vendor"]
            ],
        ),
    )
    led = report["ledger"]
    body += section(
        "Recovery ledger",
        table(
            [
                "Type",
                "Vendor",
                "Status",
                "Amount",
                "Attested by",
                "At",
                "Reason",
            ],
            [
                [
                    r["type"],
                    r["vendor"],
                    r["to_status"],
                    r["amount"],
                    r["attested_by"],
                    r["at"],
                    r["reason"],
                ]
                for r in led["rows"]
            ],
        )
        + kv(
            [
                ("Recovered", led["totals"]["recovered"]),
                ("Prevented", led["totals"]["prevented"]),
                ("Written off", led["totals"]["written_off"]),
                ("Fee on recovered", led["fee"]["fee_recovered"]),
                ("Fee on prevented", led["fee"]["fee_prevented"]),
                ("Gross fee", led["fee"]["gross"]),
                ("After cap", led["fee"]["capped"]),
                ("Fee due", led["fee"]["due"]),
            ]
        ),
    )
    return page(f"Trust Audit: {audit.get('name') or ''}", body)
