"""Trust events sent to a tenant's endpoints (PRD §9 webhooks).

`outbox.emit` writes one row per subscribed endpoint inside the session of
the change that caused the event, so an event exists exactly when its change
committed; the worker's sweep (`deliver.due`) sends them.
"""

EVENTS = (
    "finding.created",
    "finding.decided",
    "hold.placed",
    "hold.lifted",
    "ping",
)
