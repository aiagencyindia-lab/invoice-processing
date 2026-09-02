"""The columns the approved-invoice sheet can show, sort and filter by.

One catalogue, served to the grid and used to build the query. The grid renders
whatever the server says exists, the same way the settings form is generated
from `tenant_settings.SETTABLE` -- so a column the query cannot sort by can
never appear with a sort arrow on it, and a saved view naming a column that no
longer exists is caught on write rather than breaking a page later.

`source` says where a value comes from, because three kinds live side by side:

* ``column``   -- a typed column on `invoices`, written by the pipeline.
* ``workflow`` -- a key inside the workflow artifact, written when a person
                  approves or exports. Queryable through a JSON path.
* ``derived``  -- computed by the query itself (quality flags, edit counts).

Nothing here is written by the sheet. Every value already exists because the
existing pipeline and approval flow put it there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    type: str  # text | number | money | date | timestamp | list | boolean
    source: str  # column | workflow | derived
    sortable: bool = True
    filterable: bool = True
    # Shown in the grid by default. The rest are available from the column
    # menu -- a sheet that opens with thirty columns is unreadable.
    default: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "source": self.source,
            "sortable": self.sortable,
            "filterable": self.filterable,
            "default": self.default,
        }


COLUMNS: tuple[Column, ...] = (
    Column("case_id", "Case", "text", "column", default=False),
    Column("vendor_name", "Vendor", "text", "column", default=True),
    Column("invoice_number", "Invoice no.", "text", "column", default=True),
    Column("invoice_date", "Invoice date", "date", "column", default=True),
    Column("due_date", "Due date", "date", "column"),
    Column("po_number", "PO", "text", "column", default=True),
    Column("currency", "Currency", "text", "column", default=True),
    Column("total_ex_tax", "Subtotal", "money", "column"),
    Column("tax_amount", "Tax", "money", "column", default=True),
    Column("total_inc_tax", "Total", "money", "column", default=True),
    Column("vendor_tax_id", "Vendor tax ID", "text", "column"),
    Column(
        "vendor_address", "Vendor address", "text", "column", sortable=False
    ),
    Column("payment_terms", "Terms", "text", "column"),
    Column("bank_account_name", "Bank account name", "text", "column"),
    Column("bank_account_number", "Bank account", "text", "column"),
    Column("bank_code", "Bank code", "text", "column"),
    Column("decision", "Pipeline decision", "text", "column"),
    Column("routing", "Routing", "text", "column"),
    Column("min_confidence", "Confidence", "number", "column", default=True),
    Column("status", "Status", "text", "column"),
    # Written by the approval flow into the workflow artifact.
    Column("approved_at", "Approved", "timestamp", "workflow", default=True),
    Column("approved_by", "Approved by", "text", "workflow", default=True),
    Column("exported_at", "Exported", "timestamp", "workflow"),
    Column("export_target", "Exported to", "text", "workflow"),
    Column("external_id", "External ID", "text", "workflow"),
    # Computed by the query.
    Column(
        "quality_flags",
        "Quality",
        "list",
        "derived",
        sortable=False,
        default=True,
    ),
    Column(
        "corrected_fields",
        "Edited",
        "number",
        "derived",
        filterable=False,
        default=True,
    ),
)

BY_KEY: dict[str, Column] = {column.key: column for column in COLUMNS}

# Where each workflow column lives inside the artifact. The approval flow writes
# `decision`/`decided_by`/`decided_at`; the export flow adds the rest.
WORKFLOW_PATHS: dict[str, str] = {
    "approved_at": "decided_at",
    "approved_by": "decided_by",
    "exported_at": "exported_at",
    "export_target": "export_target",
    "external_id": "external_id",
}

# Quality flags, each computed at read time from data that already exists. The
# label is what a reviewer sees on the chip; the description explains it in the
# filter menu, because "po_mismatch" is not self-explanatory to a new user.
QUALITY_FLAGS: dict[str, str] = {
    "duplicate_number": "Another invoice from this vendor carries this number",
    "po_mismatch": "The purchase order check failed",
    "vendor_mismatch": "Addressed to a name outside your organisation",
    "totals_mismatch": "Total does not equal subtotal plus tax",
    "lines_mismatch": "Line items do not sum to the subtotal",
    "tax_mismatch": "Tax does not match the configured rate",
    "missing_po": "No purchase order quoted",
    "missing_tax": "No tax amount stated",
    "missing_tax_id": "No vendor tax ID",
    "missing_due_date": "No due date",
    "low_confidence": "At least one field could not be corroborated",
    "no_text_layer": "The document has no readable text, so nothing was checked",
    "high_value": "At or above the configured high-value threshold",
    "edited": "A reviewer changed a field after extraction",
}

# The validation steps behind the mismatch flags, so the mapping lives next to
# the flags rather than inside a query.
FLAG_STEPS: dict[str, str] = {
    "po_mismatch": "2.2",
    "vendor_mismatch": "1.2",
    "totals_mismatch": "4.1",
    "lines_mismatch": "4.2",
    "tax_mismatch": "4.4",
}


def catalogue() -> list[dict]:
    return [column.to_dict() for column in COLUMNS]


def is_sortable(key: str) -> bool:
    column = BY_KEY.get(key)
    return bool(column and column.sortable)
