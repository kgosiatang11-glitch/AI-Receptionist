"""C5 -- cross-tenant write isolation on real PostgreSQL.

Runs the same adversarial checks as the SQLite module against the migrated
schema (native ``uuid`` columns, real FKs, FORCE RLS).  Skipped without
``TEST_DATABASE_URL`` (see ``test_tenant_suspension_postgres.py``); the target
database is wiped, so its name must contain "test".

``RlsProbeTests`` additionally connects as a NON-privileged role -- a superuser
or BYPASSRLS role skips RLS entirely -- to show precisely what RLS does and does
not protect, which is why the application-level ownership check is required.

``rbac._is_email_confirmed`` is patched to True: on PostgreSQL it calls the
Supabase-side ``public.is_email_confirmed`` function, absent from this test DB.
"""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from smartdesk.extensions import db
from smartdesk.models import Conversation, Tenant
from tests.cross_tenant_checks import C5Fixtures, CrossTenantWriteChecks
from tests.test_whatsapp_idempotency_postgres import PostgresWebhookTestCase

CONFIRMED = patch("smartdesk.security.rbac._is_email_confirmed", return_value=True)


class _PgWorld:
    def setUp(self) -> None:
        super().setUp()
        CONFIRMED.start()
        self.addCleanup(CONFIRMED.stop)
        self.ta = self.tenant                      # "Business A" from the base class
        self.tb = Tenant(slug="b", name="Business B")
        db.session.add(self.tb)
        db.session.flush()
        self.build_c5_world()


class CrossTenantWritePgTests(_PgWorld, CrossTenantWriteChecks, PostgresWebhookTestCase):

    def test_postgres_composite_fk_blocks_cross_tenant_channel(self):
        """PostgreSQL must reject a conversation referencing another tenant's channel."""
        conv = db.session.get(Conversation, self.conv_a_id)

        conv.channel_id = self.chan_b_id
        conv.human_takeover = True

        with self.assertRaises(IntegrityError):
            db.session.commit()

        db.session.rollback()


class RlsProbeTests(_PgWorld, C5Fixtures, PostgresWebhookTestCase):
    ROLE = "c5_rls_probe"

    def setUp(self) -> None:
        super().setUp()
        self._drop_role()   # a role left behind by an interrupted run holds grants
        with db.engine.begin() as conn:
            conn.execute(text(f"CREATE ROLE {self.ROLE} NOSUPERUSER NOBYPASSRLS"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {self.ROLE}"))
            conn.execute(text(
                f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO {self.ROLE}"))
        self.addCleanup(self._drop_role)

    def _drop_role(self) -> None:
        # addCleanup runs after tearDown has popped the app context.
        with self.app.app_context():
            db.session.remove()
            with db.engine.begin() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": self.ROLE}
                ).first()
                if exists:
                    conn.execute(text(f"DROP OWNED BY {self.ROLE}"))
                    conn.execute(text(f"DROP ROLE {self.ROLE}"))

    def _as_tenant(self, conn, tenant_id) -> None:
        conn.execute(text(f"SET LOCAL ROLE {self.ROLE}"))
        conn.execute(text("SELECT set_config('app.current_tenant_id', :t, true)"),
                     {"t": tenant_id})

    def test_probe_role_really_is_subject_to_rls(self):
        with db.engine.begin() as conn:
            self._as_tenant(conn, self.ta_id)
            ids = {str(r[0]) for r in conn.execute(text("SELECT id FROM customers"))}
        self.assertIn(self.cust_a_id, ids)
        self.assertNotIn(self.cust_b_id, ids)       # RLS hides B's customer from A

    def test_rls_refuses_writing_a_row_into_another_tenant(self):
        with self.assertRaises(DBAPIError):
            with db.engine.begin() as conn:
                self._as_tenant(conn, self.ta_id)
                conn.execute(text(
                    "INSERT INTO bookings (id, tenant_id, status, source, is_test_data,"
                    " created_at, updated_at) VALUES (:i, :t, 'pending', 'staff', false,"
                    " now(), now())"), {"i": str(uuid.uuid4()), "t": self.tb_id})

    def test_composite_fk_stops_a_cross_tenant_foreign_key(self):
        """The database-level tenant-scoped FK blocks cross-tenant references."""
        booking_id = str(uuid.uuid4())

        with self.assertRaises(DBAPIError):
            with db.engine.begin() as conn:
                self._as_tenant(conn, self.ta_id)
                conn.execute(text(
                    "INSERT INTO bookings (id, tenant_id, customer_id, status, source,"
                    " is_test_data, created_at, updated_at) VALUES (:i, :t, :c, 'pending',"
                    " 'staff', false, now(), now())"),
                    {
                        "i": booking_id, 
                        "t": self.ta_id, 
                        "c": self.cust_b_id,
                        
                    },
                )


if __name__ == "__main__":
    unittest.main()
