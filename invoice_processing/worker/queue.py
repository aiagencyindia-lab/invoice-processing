"""The work queue.

Ingestion must return as soon as a document is durably stored. Extraction takes
tens of seconds and calls a model that sometimes stalls; holding an HTTP request
open across that is how an upload times out and a vendor re-sends the invoice.
So ingestion stores, enqueues, and returns -- and this is the seam between the
two halves.

The properties that matter are consequences of one fact: **delivery is
at-least-once**. Pub/Sub will redeliver a message whose ack is lost, and a
worker that crashes after processing but before acking has done exactly that.
So:

* the handler must be idempotent -- see `runner.py`, where that is enforced
  rather than hoped for;
* a message must only be acked once its work is durable, or a crash silently
  drops an invoice;
* a message that always fails must stop being redelivered, or one malformed PDF
  blocks the subscription forever.

`LocalQueue` is a directory of JSON files. It is not a toy: it gives
development and the whole test suite the same at-least-once semantics, including
redelivery and attempt counting, without a Pub/Sub emulator.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# How long a pulled message stays invisible before it is redelivered. Long
# enough for an extraction to finish; short enough that a crashed worker's
# messages come back within a coffee break.
DEFAULT_ACK_DEADLINE_SECONDS = 600

# How long an enqueue waits for Pub/Sub to confirm. It runs on the request
# path, and a healthy broker answers in milliseconds; past this the caller's
# own error handling takes over. The client keeps retrying in the background,
# so a timed-out message may still arrive later: never compensate for it.
PUBLISH_TIMEOUT_SECONDS = 5

# A batch of publishes on one request stops trying after this many failures
# in a row: one stall is retried past, but a dead broker must not cost a
# timeout per item.
OUTAGE_AFTER_FAILURES = 2


@dataclass
class Message:
    """One unit of work.

    `delivery_attempt` is what makes poison-message handling possible: without a
    count there is no way to distinguish "retry this" from "this will never
    succeed".
    """

    id: str
    payload: dict[str, Any]
    delivery_attempt: int = 1
    ack_id: str = ""
    attributes: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Queue(Protocol):
    def publish(self, payload: dict[str, Any], **attributes: str) -> str: ...

    def pull(self, max_messages: int = 1) -> list[Message]: ...

    def ack(self, message: Message) -> None: ...

    def nack(self, message: Message) -> None: ...

    def depth(self) -> int | None: ...

    def dead_letters(self) -> list[dict]: ...


class LocalQueue:
    """A directory-backed queue with real at-least-once semantics.

    Each message is a file. Pulling renames it into `leased/` with a deadline;
    a lease that expires returns the message to the queue with its attempt count
    incremented -- the same redelivery behaviour Pub/Sub has, which is the
    behaviour the worker has to be correct against.
    """

    def __init__(
        self,
        root: Path | str,
        ack_deadline_seconds: int = DEFAULT_ACK_DEADLINE_SECONDS,
        clock=time.time,
    ):
        self.root = Path(root)
        self.ready = self.root / "ready"
        self.leased = self.root / "leased"
        self.dead = self.root / "dead"
        for directory in (self.ready, self.leased, self.dead):
            directory.mkdir(parents=True, exist_ok=True)
        self.ack_deadline_seconds = ack_deadline_seconds
        # Injectable so tests can expire a lease without sleeping for it.
        self.clock = clock

    # -- Queue ------------------------------------------------------------

    def publish(self, payload: dict[str, Any], **attributes: str) -> str:
        message_id = str(uuid.uuid4())
        record = {
            "id": message_id,
            "payload": payload,
            "attributes": attributes,
            "delivery_attempt": 0,
            "published_at": self.clock(),
        }
        self._write(self.ready / f"{message_id}.json", record)
        return message_id

    def pull(self, max_messages: int = 1) -> list[Message]:
        self._reclaim_expired()
        out: list[Message] = []
        for path in sorted(self.ready.glob("*.json"))[:max_messages]:
            record = self._read(path)
            if record is None:
                # Unparseable: it can never succeed, so it goes straight to the
                # dead letter directory instead of being retried forever.
                path.rename(self.dead / path.name)
                continue
            record["delivery_attempt"] = record.get("delivery_attempt", 0) + 1
            record["lease_expires_at"] = (
                self.clock() + self.ack_deadline_seconds
            )
            target = self.leased / path.name
            self._write(target, record)
            path.unlink()
            out.append(
                Message(
                    id=record["id"],
                    payload=record["payload"],
                    delivery_attempt=record["delivery_attempt"],
                    ack_id=str(target),
                    attributes=record.get("attributes", {}),
                )
            )
        return out

    def ack(self, message: Message) -> None:
        path = Path(message.ack_id)
        if path.exists():
            path.unlink()

    def nack(self, message: Message) -> None:
        """Return it for redelivery now, rather than waiting out the lease."""
        path = Path(message.ack_id)
        if path.exists():
            path.rename(self.ready / path.name)

    # -- dead letters ------------------------------------------------------

    def dead_letter(self, message: Message, reason: str) -> None:
        """Park a message that will never succeed, keeping why.

        Deleting it would lose the document silently; leaving it in the queue
        would block every message behind it. Parking it does neither.
        """
        path = Path(message.ack_id)
        record = self._read(path) or {
            "id": message.id,
            "payload": message.payload,
        }
        record["dead_letter_reason"] = reason
        record["dead_lettered_at"] = self.clock()
        self._write(self.dead / f"{message.id}.json", record)
        if path.exists():
            path.unlink()
        logger.error("Dead-lettered %s: %s", message.id, reason)

    def dead_letters(self) -> list[dict]:
        return [
            record
            for record in (
                self._read(p) for p in sorted(self.dead.glob("*.json"))
            )
            if record is not None
        ]

    def depth(self) -> int:
        return len(list(self.ready.glob("*.json")))

    # -- internals ---------------------------------------------------------

    def _reclaim_expired(self) -> None:
        """A worker that died mid-message must not strand it."""
        now = self.clock()
        for path in self.leased.glob("*.json"):
            record = self._read(path)
            if record is None or record.get("lease_expires_at", 0) <= now:
                path.rename(self.ready / path.name)

    @staticmethod
    def _write(path: Path, record: dict) -> None:
        # Temp file plus rename: a reader must never see a half-written message.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, default=str), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


class PubSubQueue:
    """Google Cloud Pub/Sub.

    Pull rather than push: a push subscription needs a public endpoint and OIDC
    verification on it, which is a second authentication surface to get right.
    A pulling worker needs neither, and scales on Cloud Run the same way.
    """

    def __init__(
        self,
        topic: str | None = None,
        subscription: str | None = None,
        project_id: str | None = None,
        publisher=None,
        subscriber=None,
    ):
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.topic_name = topic or os.getenv(
            "PUBSUB_TOPIC", "invoice-documents"
        )
        self.subscription_name = subscription or os.getenv(
            "PUBSUB_SUBSCRIPTION", "invoice-documents-worker"
        )
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for Pub/Sub.")
        self._publisher = publisher
        self._subscriber = subscriber

    @property
    def publisher(self):
        if self._publisher is None:
            from google.cloud import pubsub_v1  # noqa: PLC0415

            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    @property
    def subscriber(self):
        if self._subscriber is None:
            from google.cloud import pubsub_v1  # noqa: PLC0415

            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    def depth(self) -> int | None:
        """Unknown, honestly.

        Pub/Sub does not expose a backlog size on the data plane; the number
        lives in Cloud Monitoring as `num_undelivered_messages`. Returning 0
        would read as "nothing waiting", which is a different and much more
        reassuring claim than "we cannot see". None renders as an em dash.
        """
        return None

    def dead_letters(self) -> list[dict]:
        """Empty unless a dead-letter topic is attached to the subscription.

        Pub/Sub redelivers on its own until `max_delivery_attempts`, and only
        then moves a message aside -- and only when a dead-letter topic exists.
        This deployment has none, so there is nothing to list rather than
        nothing wrong.
        """
        return []

    @property
    def topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.topic_name}"

    @property
    def subscription_path(self) -> str:
        return (
            f"projects/{self.project_id}/subscriptions/{self.subscription_name}"
        )

    def publish(self, payload: dict[str, Any], **attributes: str) -> str:
        future = self.publisher.publish(
            self.topic_path,
            json.dumps(payload, default=str).encode("utf-8"),
            **attributes,
        )
        return future.result(timeout=PUBLISH_TIMEOUT_SECONDS)

    def pull(self, max_messages: int = 1) -> list[Message]:
        response = self.subscriber.pull(
            request={
                "subscription": self.subscription_path,
                "max_messages": max_messages,
            },
            timeout=30,
        )
        out = []
        for received in response.received_messages:
            try:
                payload = json.loads(received.message.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            out.append(
                Message(
                    id=received.message.message_id,
                    payload=payload,
                    # Pub/Sub only supplies this when the subscription has a
                    # dead-letter policy; 1 is the safe assumption otherwise.
                    delivery_attempt=getattr(received, "delivery_attempt", 1)
                    or 1,
                    ack_id=received.ack_id,
                    attributes=dict(received.message.attributes),
                )
            )
        return out

    def ack(self, message: Message) -> None:
        self.subscriber.acknowledge(
            request={
                "subscription": self.subscription_path,
                "ack_ids": [message.ack_id],
            }
        )

    def nack(self, message: Message) -> None:
        self.subscriber.modify_ack_deadline(
            request={
                "subscription": self.subscription_path,
                "ack_ids": [message.ack_id],
                "ack_deadline_seconds": 0,
            }
        )

    def dead_letter(self, message: Message, reason: str) -> None:
        """Pub/Sub routes to the dead-letter topic itself, by attempt count.

        Acking here is what stops redelivery; the subscription's dead-letter
        policy is what preserved the message. Both are required -- a
        subscription configured without that policy would drop it.
        """
        logger.error("Dead-lettering %s: %s", message.id, reason)
        self.ack(message)


def get_queue(root: Path | str | None = None) -> Queue:
    """Pub/Sub when configured, a local directory otherwise."""
    if os.getenv("PUBSUB_TOPIC") and os.getenv("GOOGLE_CLOUD_PROJECT"):
        return PubSubQueue()
    from .. import paths  # noqa: PLC0415

    return LocalQueue(root or paths.state_dir() / "queue")
