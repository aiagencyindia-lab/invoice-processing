"""Google Cloud Storage implementation of the BlobStore port.

Documents are the evidence behind every decision: the invoice a rejection cites,
the `.eml` an ingestion came from, the page images a reviewer looks at. They are
written once and never modified.

Two properties matter more than the API surface:

* **Tenant prefixing.** Every key is written under ``t/<tenant_id>/``, and a key
  that tries to climb out is refused. In a bucket shared by tenants, a key like
  ``../other-tenant/invoice.pdf`` is the whole isolation failure, and it costs
  three lines to prevent.
* **Signed URLs, not proxying.** A reviewer's browser fetches the PDF straight
  from GCS with a short-lived signed URL, so document bytes never pass through
  the API tier. That is both faster and one less place a document can leak from.

The class satisfies the same protocol as `LocalBlobStore`, so development and
tests keep using the filesystem and nothing in the engines changes.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

DEFAULT_SIGNED_URL_TTL = 3600


class GcsBlobStore:
    """Blobs in a GCS bucket, namespaced per tenant.

    `client` is injectable so tests can supply a fake; in production it is a
    `google.cloud.storage.Client` using the service's own credentials.
    """

    def __init__(
        self,
        bucket_name: str | None = None,
        tenant_id: str = "default",
        client: Any = None,
        prefix: str | None = None,
    ):
        self.bucket_name = bucket_name or os.getenv("GCS_BUCKET", "")
        if not self.bucket_name:
            raise ValueError(
                "No GCS bucket configured. Set GCS_BUCKET or pass bucket_name."
            )
        self.tenant_id = tenant_id
        # Explicit prefix wins, so an operator can pin a layout; otherwise every
        # key lands under the tenant's own namespace.
        self.prefix = prefix if prefix is not None else f"t/{tenant_id}"
        self._client = client
        self._bucket = None

    # -- internals ---------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            from google.cloud import storage  # noqa: PLC0415

            self._client = storage.Client()
        return self._client

    @property
    def bucket(self):
        if self._bucket is None:
            self._bucket = self.client.bucket(self.bucket_name)
        return self._bucket

    def _key(self, key: str) -> str:
        """Namespace a key, refusing anything that escapes the tenant prefix.

        `..` in an object name is not a path traversal in GCS the way it is on a
        filesystem -- GCS treats it as a literal -- but the key often becomes a
        filesystem path later (a download, a cache, a temp file), and a key that
        reads as another tenant's namespace is a bug waiting for that moment.
        """
        cleaned = str(key or "").strip().lstrip("/")
        if not cleaned:
            raise ValueError("Blob key must not be empty")
        parts = [p for p in cleaned.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"Key escapes the tenant prefix: {key!r}")
        return (
            f"{self.prefix}/{'/'.join(parts)}"
            if self.prefix
            else "/".join(parts)
        )

    # -- BlobStore ---------------------------------------------------------

    def get(self, key: str) -> bytes:
        return self.bucket.blob(self._key(key)).download_as_bytes()

    def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        blob = self.bucket.blob(self._key(key))
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{self._key(key)}"

    def exists(self, key: str) -> bool:
        return bool(self.bucket.blob(self._key(key)).exists())

    def signed_url(
        self, key: str, ttl_seconds: int = DEFAULT_SIGNED_URL_TTL
    ) -> str:
        """A time-limited read URL.

        Read-only and short-lived by default: the reviewer UI needs to display a
        document for the length of a review, not grant durable access to it.
        """
        return self.bucket.blob(self._key(key)).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=ttl_seconds),
            method="GET",
        )

    # -- extras the platform needs ----------------------------------------

    def delete(self, key: str) -> None:
        """Only for retention and tenant offboarding.

        Documents are immutable evidence during their retention period; nothing
        in the processing path should call this.
        """
        self.bucket.blob(self._key(key)).delete()

    def list_keys(self, subpath: str = "") -> list[str]:
        """Keys under the tenant prefix, returned without it.

        Listing is scoped to the tenant by construction: an unprefixed list over
        a shared bucket would enumerate every tenant's documents.
        """
        prefix = self._key(subpath) if subpath else f"{self.prefix}/"
        blobs = self.client.list_blobs(self.bucket_name, prefix=prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        return sorted(blob.name[strip:] for blob in blobs)


def for_tenant(tenant_id: str, **kwargs) -> GcsBlobStore:
    """Convenience constructor: one store per tenant."""
    return GcsBlobStore(tenant_id=tenant_id, **kwargs)
