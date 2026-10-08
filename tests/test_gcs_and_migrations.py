"""GCS blob storage and the schema migration.

Two properties carry the weight here.

For storage: in a bucket shared by tenants, every key must land under that
tenant's prefix, and a key that tries to climb out must be refused. A key like
`../other-tenant/invoice.pdf` is the entire isolation failure, and GCS will not
stop it -- it treats `..` as a literal character, so the check has to be ours.

For the migration: it must apply and roll back cleanly, and it must carry the
row-level security policies. A schema that deploys without them is a
multi-tenant database with no database-level isolation at all.

No test touches GCS; a fake client records calls.
"""

import glob
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect

from invoice_processing.api import service
from invoice_processing.db import migrate, session
from invoice_processing.db.models import TENANT_TABLES
from invoice_processing.gcs import GcsBlobStore, for_tenant
from invoice_processing.ports import BlobStore
from invoice_processing.worker import __main__ as worker_main

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fake GCS
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, name, store):
        self.name = name
        self._store = store

    def upload_from_string(self, data, content_type="application/octet-stream"):
        self._store.objects[self.name] = (data, content_type)

    def download_as_bytes(self):
        if self.name not in self._store.objects:
            raise FileNotFoundError(self.name)
        return self._store.objects[self.name][0]

    def exists(self):
        return self.name in self._store.objects

    def delete(self):
        self._store.objects.pop(self.name, None)

    def generate_signed_url(self, version, expiration, method):
        self._store.signed.append((self.name, expiration, method))
        return f"https://signed.example/{self.name}?exp={int(expiration.total_seconds())}"


class FakeBucket:
    def __init__(self, store):
        self._store = store

    def blob(self, name):
        return FakeBlob(name, self._store)


class FakeGcsClient:
    def __init__(self):
        self.objects = {}
        self.signed = []

    def bucket(self, name):
        return FakeBucket(self)

    def list_blobs(self, bucket_name, prefix=""):
        return [
            FakeBlob(name, self)
            for name in sorted(self.objects)
            if name.startswith(prefix)
        ]


@pytest.fixture
def gcs():
    client = FakeGcsClient()
    return GcsBlobStore("test-bucket", tenant_id="acme", client=client), client


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """The property whose failure is a breach."""

    def test_keys_land_under_the_tenant_prefix(self, gcs):
        store, client = gcs
        store.put("case_005/invoice.pdf", b"%PDF")
        assert "t/acme/case_005/invoice.pdf" in client.objects

    def test_two_tenants_do_not_collide(self):
        client = FakeGcsClient()
        acme = GcsBlobStore("b", tenant_id="acme", client=client)
        globex = GcsBlobStore("b", tenant_id="globex", client=client)

        acme.put("case_001/invoice.pdf", b"acme bytes")
        globex.put("case_001/invoice.pdf", b"globex bytes")

        assert acme.get("case_001/invoice.pdf") == b"acme bytes"
        assert globex.get("case_001/invoice.pdf") == b"globex bytes"

    @pytest.mark.parametrize(
        "key",
        [
            "../globex/invoice.pdf",
            "case_001/../../globex/invoice.pdf",
            "..",
            "a/../../b",
        ],
    )
    def test_keys_cannot_escape_the_prefix(self, gcs, key):
        """GCS treats '..' as a literal, so this check has to be ours."""
        store, _ = gcs
        with pytest.raises(ValueError, match="escapes"):
            store.put(key, b"x")

    def test_leading_slash_does_not_escape(self, gcs):
        store, client = gcs
        store.put("/case_001/x.pdf", b"x")
        assert "t/acme/case_001/x.pdf" in client.objects

    def test_empty_key_is_rejected(self, gcs):
        store, _ = gcs
        with pytest.raises(ValueError, match="must not be empty"):
            store.put("", b"x")

    def test_listing_is_scoped_to_the_tenant(self):
        """An unprefixed list would enumerate every tenant's documents."""
        client = FakeGcsClient()
        acme = GcsBlobStore("b", tenant_id="acme", client=client)
        globex = GcsBlobStore("b", tenant_id="globex", client=client)

        acme.put("case_001/a.pdf", b"1")
        globex.put("case_002/b.pdf", b"2")

        assert acme.list_keys() == ["case_001/a.pdf"]
        assert globex.list_keys() == ["case_002/b.pdf"]


class TestBlobStoreContract:
    def test_satisfies_the_port(self, gcs):
        store, _ = gcs
        assert isinstance(store, BlobStore)

    def test_round_trip(self, gcs):
        store, _ = gcs
        store.put(
            "case_005/invoice.pdf", b"%PDF-1.4", content_type="application/pdf"
        )
        assert store.exists("case_005/invoice.pdf")
        assert store.get("case_005/invoice.pdf") == b"%PDF-1.4"

    def test_put_returns_a_gs_uri(self, gcs):
        store, _ = gcs
        uri = store.put("case_005/invoice.pdf", b"x")
        assert uri == "gs://test-bucket/t/acme/case_005/invoice.pdf"

    def test_missing_key(self, gcs):
        store, _ = gcs
        assert store.exists("nope.pdf") is False

    def test_content_type_is_preserved(self, gcs):
        store, client = gcs
        store.put("a.pdf", b"x", content_type="application/pdf")
        assert client.objects["t/acme/a.pdf"][1] == "application/pdf"


class TestSignedUrls:
    def test_url_is_read_only_and_expiring(self, gcs):
        """Long-lived or writable URLs turn a review link into durable access."""
        store, client = gcs
        store.put("case_005/invoice.pdf", b"x")
        store.signed_url("case_005/invoice.pdf", ttl_seconds=300)

        name, expiration, method = client.signed[0]
        assert name == "t/acme/case_005/invoice.pdf"
        assert method == "GET", "signed URLs must not permit writes"
        assert expiration.total_seconds() == 300

    def test_default_ttl_is_bounded(self, gcs):
        store, client = gcs
        store.signed_url("a.pdf")
        assert client.signed[0][1].total_seconds() <= 3600


class TestConfiguration:
    def test_missing_bucket_fails_loudly(self, monkeypatch):
        """Better than silently writing into a default bucket."""
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        with pytest.raises(ValueError, match="No GCS bucket"):
            GcsBlobStore()

    def test_bucket_from_environment(self, monkeypatch):
        monkeypatch.setenv("GCS_BUCKET", "from-env")
        assert GcsBlobStore(client=FakeGcsClient()).bucket_name == "from-env"

    def test_for_tenant_helper(self):
        store = for_tenant("globex", bucket_name="b", client=FakeGcsClient())
        assert store.prefix == "t/globex"


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _alembic(command: str, db_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={
            **dict(__import__("os").environ),
            "DATABASE_URL": f"sqlite:///{db_path}",
        },
    )


class TestMigration:
    def test_upgrade_then_downgrade(self, tmp_path):
        """A migration that cannot roll back is a one-way door in production."""
        db = tmp_path / "m.db"

        up = _alembic("upgrade head", db)
        assert up.returncode == 0, up.stderr

        tables = {
            row[0]
            for row in sqlite3.connect(db).execute(
                "select name from sqlite_master where type='table'"
            )
        }
        for expected in ("tenants", "invoices", "audit_events", "rules"):
            assert expected in tables, f"{expected} not created"

        down = _alembic("downgrade base", db)
        assert down.returncode == 0, down.stderr

        remaining = {
            row[0]
            for row in sqlite3.connect(db).execute(
                "select name from sqlite_master where type='table'"
            )
        }
        assert remaining == {"alembic_version"}, f"left behind: {remaining}"

    def test_migration_carries_rls(self):
        """A schema deployed without RLS has no database-level isolation."""
        path = glob.glob(
            str(
                REPO
                / "invoice_processing/db/migrations/versions/*_initial_schema.py"
            )
        )[0]
        source = Path(path).read_text()
        assert "enable_rls_statements" in source
        assert "revoke_audit_mutation_statements" in source
        assert 'dialect.name == "postgresql"' in source, (
            "RLS must be guarded: these statements are Postgres-only"
        )

    def test_approval_json_columns_are_jsonb_on_postgres(self, monkeypatch):
        """The models declare JSONB on Postgres; a migration creating JSON
        is drift autogenerate flags and the JSONB operators would not work."""
        import importlib.util  # noqa: PLC0415
        from types import SimpleNamespace  # noqa: PLC0415

        import sqlalchemy as sa  # noqa: PLC0415
        from sqlalchemy.dialects import postgresql  # noqa: PLC0415

        path = glob.glob(
            str(
                REPO
                / "invoice_processing/db/migrations/versions/f2c7a19d8e43_*.py"
            )
        )[0]
        spec = importlib.util.spec_from_file_location("approval_mig", path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        tables = {}
        monkeypatch.setattr(
            mig,
            "op",
            SimpleNamespace(
                create_table=lambda name, *cols: tables.update({name: cols}),
                create_index=lambda *a, **k: None,
                add_column=lambda *a, **k: None,
                get_bind=lambda: SimpleNamespace(
                    dialect=SimpleNamespace(name="sqlite")
                ),
            ),
        )
        mig.upgrade()
        json_cols = {
            f"{t}.{c.name}": c.type.dialect_impl(postgresql.dialect())
            for t, cols in tables.items()
            for c in cols
            if isinstance(c, sa.Column) and isinstance(c.type, sa.JSON)
        }
        assert set(json_cols) == {
            "approver_groups.members",
            "approval_rules.match",
            "approval_rules.steps",
        }
        for name, impl in json_cols.items():
            assert isinstance(impl, postgresql.JSONB), name

    def test_no_connection_string_is_committed(self):
        """The URL comes from DATABASE_URL, so no password reaches the repo."""
        ini = (REPO / "alembic.ini").read_text()
        assert "sqlalchemy.url =" not in ini
        assert "password" not in ini.lower()


class TestMigrationsRunAtStartup:
    """Nothing applied them. The image starts uvicorn, the entrypoint only
    writes a credential file, and neither process ran Alembic -- so the
    production schema exists because somebody ran it by hand once, and the
    next migration would not have applied itself.
    """

    def test_an_empty_database_is_brought_to_head(self, tmp_path, monkeypatch):
        url = f"sqlite:///{tmp_path / 'fresh.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setattr(session, "_engine", None)

        assert migrate.current_revision() is None

        revision = migrate.upgrade_to_head()

        assert revision is not None
        assert migrate.current_revision() == revision

    def test_running_it_twice_changes_nothing(self, tmp_path, monkeypatch):
        """Every replica runs this on deploy."""
        url = f"sqlite:///{tmp_path / 'twice.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setattr(session, "_engine", None)

        first = migrate.upgrade_to_head()
        second = migrate.upgrade_to_head()

        assert first == second

    def test_it_can_be_turned_off(self, tmp_path, monkeypatch):
        """For a deployment that applies migrations in a release step."""
        url = f"sqlite:///{tmp_path / 'off.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("RUN_MIGRATIONS", "0")
        monkeypatch.setattr(session, "_engine", None)

        assert migrate.upgrade_to_head() is None
        assert migrate.current_revision() is None

    def test_the_tables_the_app_queries_exist_afterwards(
        self, tmp_path, monkeypatch
    ):
        url = f"sqlite:///{tmp_path / 'tables.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setattr(session, "_engine", None)

        migrate.upgrade_to_head()

        present = set(inspect(session.get_engine()).get_table_names())
        assert set(TENANT_TABLES) <= present


# Run in a child process. Each child marks itself ready, waits for the other,
# then migrates -- so both reach the DDL together, as the API and the worker do
# when the local stack starts.
_RACE_CHILD = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
from invoice_processing.db import migrate
gate = Path(os.environ["GATE"])
(gate / os.environ["NAME"]).touch()
deadline = time.time() + 60
while len(list(gate.iterdir())) < 2 and time.time() < deadline:
    time.sleep(0.01)
print(migrate.upgrade_to_head())
"""


class TestConcurrentSqliteMigration:
    def test_two_processes_migrating_at_once_both_succeed(self, tmp_path):
        """The local stack starts the API and the worker together on one
        SQLite file. With a migration pending, the loser of the race died on
        `duplicate column name` halfway through DDL the winner had applied."""
        db = tmp_path / "race.db"
        behind = _alembic("upgrade head-1", db)
        assert behind.returncode == 0, behind.stderr
        heads = _alembic("heads", db).stdout.splitlines()
        head = next(line.split()[0] for line in heads if "(head)" in line)

        gate = tmp_path / "gate"
        gate.mkdir()
        env = {
            **dict(__import__("os").environ),
            "DATABASE_URL": f"sqlite:///{db}",
            "CLOUD_SQL_INSTANCE": "",
            "RUN_MIGRATIONS": "1",
            "REPO": str(REPO),
            "GATE": str(gate),
        }
        children = [
            subprocess.Popen(
                [sys.executable, "-c", _RACE_CHILD],
                cwd=REPO,
                env={**env, "NAME": name},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in ("api", "worker")
        ]
        results = [child.communicate(timeout=180) for child in children]

        for child, (out, err) in zip(children, results, strict=True):
            assert child.returncode == 0, err[-2000:]
            assert out.strip().splitlines()[-1] == head
        version = sqlite3.connect(db).execute(
            "select version_num from alembic_version"
        )
        assert [row[0] for row in version] == [head]


class TestWorkerStartup:
    def test_a_failed_migration_is_logged_and_exits_nonzero(
        self, monkeypatch, caplog
    ):
        """A worker that cannot migrate must say so, not vanish: the UI only
        reports that the worker was last seen hours ago."""

        def broken():
            raise RuntimeError("duplicate column name: human_decision")

        monkeypatch.setattr(
            worker_main, "materialise_credentials", lambda: None
        )
        monkeypatch.setattr(worker_main, "get_queue", object)
        monkeypatch.setattr(service, "_database_configured", lambda: True)
        monkeypatch.setattr(migrate, "upgrade_to_head", broken)

        with caplog.at_level("ERROR"):
            assert worker_main.main([]) == 1
        assert "could not be migrated" in caplog.text
        assert "duplicate column name" in caplog.text
