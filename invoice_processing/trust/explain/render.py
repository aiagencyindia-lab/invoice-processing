"""The evidence pack as a print-styled page. Everything goes through
`trust/html.py`, which escapes every value."""

from __future__ import annotations

from .. import html as h


def pack_html(pack: dict) -> str:
    f, x, cf = (
        pack["finding"],
        pack["explanation"],
        pack["explanation"].get("counterfactual"),
    )
    body = h.kv(
        [
            ("Finding", f.get("id")),
            ("Type", f.get("type")),
            ("Status", pack["decision"].get("status")),
            (
                "Impact",
                f"{f.get('currency') or ''} {f.get('impact') or '-'}".strip(),
            ),
            ("Confidence", f.get("confidence")),
            ("Generated", pack["generated_at"]),
            ("Pack sha256", pack["sha256"]),
            (
                "Versions",
                ", ".join(f"{k}={v}" for k, v in pack["versions"].items()),
            ),
        ]
    )
    body += h.section("What happened", f"<p>{h.escape(x['what_happened'])}</p>")
    body += h.section(
        "Why",
        "<ol>"
        + "".join(
            f'<li>{h.escape(s["text"])} <span class="muted">[{h.escape(s["node_id"])}]</span></li>'
            for s in x["why"]
        )
        + "</ol>",
    )
    body += h.section(
        "Caused by",
        h.table(
            ["Kind", "Who", "Node"],
            [
                [a["kind"], a["label"], a["node_id"] or ""]
                for a in x["caused_by"]
            ],
        ),
    )
    body += h.section(
        "Evidence",
        h.table(
            ["Node", "Kind", "Label", "Excerpt", "Source"],
            [
                [
                    e["node_id"],
                    e["kind"],
                    e["label"],
                    e["excerpt"] or "",
                    (e.get("source") or {}).get("document_url") or "",
                ]
                for e in x["evidence"]
            ],
        ),
    )
    if cf:
        body += h.section(
            "Counterfactual",
            h.kv(
                [
                    ("Payable now", cf.get("payable_now")),
                    ("Payable if correct", cf.get("payable_correct")),
                    (
                        "Delta",
                        f"{cf.get('currency') or ''} {cf.get('delta') or '-'}".strip(),
                    ),
                    ("Formula", cf.get("formula")),
                    ("Note", cf.get("note")),
                ]
            ),
        )
    body += h.section(
        "Confidence and next action",
        h.kv(
            [
                ("Confidence", x["confidence_note"]),
                ("Next action", x["next_action"]),
            ]
        )
        + "<ul>"
        + "".join(f"<li>{h.escape(w)}</li>" for w in x["would_change"])
        + "</ul>",
    )
    body += h.section(
        "Lineage",
        h.table(
            ["Stage", "Status", "Node", "Note"],
            [
                [
                    link["stage"],
                    link["status"],
                    link["node_id"] or "",
                    link["note"],
                ]
                for link in pack["lineage"]
            ],
        ),
    )
    body += h.section(
        "Decision",
        h.kv(
            [
                ("Owner", pack["decision"].get("owner_id")),
                ("Decided at", pack["decision"].get("decided_at")),
            ]
        )
        + h.table(
            ["From", "To", "Amount", "Actor", "Reason", "At"],
            [
                [
                    e.get("from_status"),
                    e.get("to_status"),
                    e.get("amount"),
                    e.get("actor"),
                    e.get("reason"),
                    e.get("at"),
                ]
                for e in pack["decision"]["events"]
            ],
        ),
    )
    body += h.section(
        "Ratings",
        h.table(
            ["Rater", "Correct", "Useful", "Note", "At"],
            [
                [
                    r["rater"],
                    r["correct"],
                    r["useful"],
                    r.get("note"),
                    r.get("at"),
                ]
                for r in pack["ratings"]
            ],
        ),
    )
    body += h.section(
        "Nodes",
        h.table(
            ["Node", "Kind", "Key", "Attributes"],
            [
                [
                    n["id"],
                    n["kind"],
                    n["natural_key"],
                    ", ".join(
                        f"{k}={v}" for k, v in (n.get("attrs") or {}).items()
                    ),
                ]
                for n in pack["nodes"]
            ],
        ),
    )
    return h.page(f"Evidence pack {f.get('type')} {f.get('id')}", body)
