"""C6 -- user assignment & membership hardening on real PostgreSQL.

Runs the shared adversarial checks against the migrated schema and adds the
things SQLite cannot show: the composite FK, ``ON DELETE SET NULL
(assigned_user_id)``, the 0008 preflight / downgrade, and a deterministic
last-owner race.  Skipped without ``TEST_DATABASE_URL`` (the DB is wiped; its
name must contain "test").
"""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from smartdesk.extensions import db
from smartdesk.models import Conversation, Lead, Membership, Tenant, User
from tests.c6_checks import C6Checks, C6Fixtures
from tests.test_whatsapp_idempotency_postgres import MIGRATIONS_DIR, PostgresWebhookTestCase

CONFIRMED = patch("smartdesk.security.rbac._is_email_confirmed", return_value=True)


class _PgWorld:
    def setUp(self) -> None:
        super().setUp()
        CONFIRMED.start()
        self.addCleanup(CONFIRMED.stop)
        self.ta = self.tenant
        self.tb = Tenant(slug="b", name="Business B")
        db.session.add(self.tb)
        db.session.flush()
        self.build_c5_world()
        self.build_c6_world()


class C6PgTests(_PgWorld, C6Checks, PostgresWebhookTestCase):
    pass


class AssigneeForeignKeyTests(_PgWorld, C6Fixtures, PostgresWebhookTestCase):
    """The database itself refuses a cross-tenant / non-member assignee."""

    def _assign(self, model, row_id, user_id):
        db.session.remove()
        row = db.session.get(model, row_id)
        row.assigned_user_id = user_id
        db.session.commit()

    def test_constraint_definition_uses_the_column_list_form(self):
        for name in ("fk_leads_assignee_member", "fk_conversations_assignee_member"):
            definition = db.session.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"
            ), {"n": name}).scalar_one()
            self.assertIn("FOREIGN KEY (tenant_id, assigned_user_id)", definition)
            self.assertIn("REFERENCES memberships(tenant_id, user_id)", definition)
            self.assertIn("ON DELETE SET NULL (assigned_user_id)", definition)

    def test_cross_tenant_assignment_is_rejected_by_the_database(self):
        for model, row_id in ((Lead, self.lead_a_id), (Conversation, self.conv_a_id)):
            with self.subTest(model=model.__name__):
                with self.assertRaises(IntegrityError):
                    self._assign(model, row_id, self.owner_b_id)
                db.session.rollback()

    def test_non_member_assignment_is_rejected_by_the_database(self):
        for model, row_id in ((Lead, self.lead_a_id), (Conversation, self.conv_a_id)):
            with self.subTest(model=model.__name__):
                with self.assertRaises(IntegrityError):
                    self._assign(model, row_id, self.owner2_a_id)   # no membership anywhere
                db.session.rollback()

    def test_same_tenant_member_is_accepted(self):
        self._assign(Lead, self.lead_a_id, self.member_a_id)
        self._assign(Conversation, self.conv_a_id, self.member_a_id)
        self.assertEqual(self.assignee(Lead, self.lead_a_id), self.member_a_id)

    def test_deleting_a_membership_nulls_only_the_assignee(self):
        self._assign(Lead, self.lead_a_id, self.member_a_id)
        self._assign(Conversation, self.conv_a_id, self.member_a_id)
        db.session.remove()
        db.session.execute(text(
            "DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"
        ), {"t": self.ta_id, "u": self.member_a_id})
        db.session.commit()
        for table in ("leads", "conversations"):
            row = db.session.execute(text(
                f"SELECT tenant_id, assigned_user_id FROM {table} WHERE tenant_id = :t"
            ), {"t": self.ta_id}).first()
            self.assertEqual(str(row.tenant_id), str(self.ta_id))       # tenant_id untouched
            self.assertIsNone(row.assigned_user_id)

    def test_membership_in_another_tenant_is_not_touched_by_clearing(self):
        self._assign(Conversation, self.conv_a_id, self.agent2_id)
        db.session.remove()
        db.session.execute(text(
            "DELETE FROM memberships WHERE tenant_id = :t AND user_id = :u"
        ), {"t": self.tb_id, "u": self.agent2_id})
        db.session.commit()
        self.assertEqual(self.assignee(Conversation, self.conv_a_id), self.agent2_id)

    def test_deleting_a_user_clears_assignments(self):
        self._assign(Lead, self.lead_a_id, self.member_a_id)
        db.session.remove()
        db.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": self.member_a_id})
        db.session.commit()
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))


class MigrationPreflightTests(PostgresWebhookTestCase):
    """0008 refuses to run over data that violates the policy, and never edits it."""

    def _migrate(self, action, revision):
        import flask_migrate

        db.session.rollback()
        db.session.remove()
        db.engine.dispose()
        getattr(flask_migrate, action)(directory=MIGRATIONS_DIR, revision=revision)
        db.engine.dispose()

    def _scalar(self, sql, **params):
        db.session.rollback()
        return db.session.execute(text(sql), params).scalar()

    def tearDown(self):
        try:
            if self._scalar("SELECT version_num FROM alembic_version") != "0008_assignment_invitations":
                self._migrate("upgrade", "head")
        finally:
            super().tearDown()

    def _seed_violation(self, kind):
        """At revision 0007: one lead assigned in a way the 0008 policy forbids."""
        tenant_id = self.tenant_id
        other = Tenant(slug="other", name="Other")
        db.session.add(other)
        mk = lambda e, **kw: User(supabase_user_id=str(uuid.uuid4()), email=e, **kw)  # noqa: E731
        outsider, viewer, inactive = mk("o@x.test"), mk("v@x.test"), mk("i@x.test", is_active=False)
        db.session.add_all([outsider, viewer, inactive])
        db.session.flush()
        db.session.add_all([
            Membership(tenant_id=other.id, user_id=outsider.id, role="owner"),
            Membership(tenant_id=tenant_id, user_id=viewer.id, role="viewer"),
            Membership(tenant_id=tenant_id, user_id=inactive.id, role="agent"),
        ])
        from smartdesk.models import Customer
        cust = Customer(tenant_id=tenant_id, phone="+26771111111")
        db.session.add(cust)
        db.session.flush()
        bad = {"outsider": outsider, "viewer": viewer, "inactive": inactive}[kind]
        lead = Lead(tenant_id=tenant_id, customer_id=cust.id, assigned_user_id=bad.id)
        db.session.add(lead)
        db.session.commit()
        return lead.id, bad.id

    def test_preflight_aborts_changes_nothing_then_succeeds_after_a_deliberate_fix(self):
        for kind in ("outsider", "viewer", "inactive"):
            with self.subTest(kind=kind):
                db.session.rollback()
                db.session.execute(text("TRUNCATE tenants, users CASCADE"))
                db.session.execute(text("INSERT INTO tenants (id, slug, name) VALUES "
                                        "(:i, 'a', 'Business A')"), {"i": self.tenant_id})
                db.session.commit()
                self._migrate("downgrade", "0007_tenant_scoped_foreign_keys")
                lead_id, user_id = self._seed_violation(kind)

                # flask-migrate logs a RuntimeError from a revision and exits(1).
                with patch("flask_migrate.log") as log, self.assertRaises(SystemExit):
                    self._migrate("upgrade", "head")
                self.assertIn("0008 aborted", str(log.error.call_args))

                # Nothing created, nothing repaired.
                self.assertEqual(self._scalar("SELECT version_num FROM alembic_version"),
                                 "0007_tenant_scoped_foreign_keys")
                self.assertFalse(self._scalar(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_leads_assignee_member')"))
                self.assertFalse(self._scalar("SELECT to_regclass('public.membership_invitations') IS NOT NULL"))
                self.assertEqual(str(self._scalar(
                    "SELECT assigned_user_id FROM leads WHERE id = :i", i=lead_id)), str(user_id))

                # A human resolves it deliberately; the same migration then applies.
                db.session.execute(text("UPDATE leads SET assigned_user_id = NULL WHERE id = :i"),
                                   {"i": lead_id})
                db.session.commit()
                self._migrate("upgrade", "head")
                self.assertEqual(self._scalar("SELECT version_num FROM alembic_version"),
                                 "0008_assignment_invitations")
                self.assertTrue(self._scalar(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_leads_assignee_member')"))

    def test_downgrade_removes_only_what_0008_added_and_round_trips(self):
        self._migrate("downgrade", "0007_tenant_scoped_foreign_keys")
        self.assertFalse(self._scalar("SELECT to_regclass('public.membership_invitations') IS NOT NULL"))
        for name in ("fk_leads_assignee_member", "fk_conversations_assignee_member"):
            self.assertFalse(self._scalar(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = :n)", n=name))
        # The C5 protections from 0007 are still present.
        self.assertTrue(self._scalar(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_conversations_tenant_id')"
            " OR to_regclass('public.conversations') IS NOT NULL"))
        self._migrate("upgrade", "head")
        self.assertEqual(self._scalar("SELECT version_num FROM alembic_version"),
                         "0008_assignment_invitations")


class LastOwnerRaceTests(_PgWorld, C6Fixtures, PostgresWebhookTestCase):
    """Two owners; one request demotes itself while another removes the co-owner.

    Deterministic: the test holds the tenant row lock, the demotion request must
    block on it, then the co-owner is deleted and committed.  The demotion must
    then see the *current* state and refuse.
    """

    def test_self_demotion_blocks_on_the_tenant_lock_then_sees_the_committed_change(self):
        co_owner = self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        mid = self.membership_id(self.ta_id, self.owner_a_id)
        headers = self.hdr_a
        result = {}

        def demote():
            with self.app.app_context():
                r = self.app.test_client().patch(
                    f"/api/v1/business/members/{mid}", headers=headers, json={"role": "agent"})
                result["status"], result["body"] = r.status_code, r.get_json()
                result["done"] = time.monotonic()
                db.session.remove()

        db.session.rollback()
        db.session.remove()
        with db.engine.connect() as conn:
            txn = conn.begin()
            conn.execute(text("SELECT id FROM tenants WHERE id = :t FOR NO KEY UPDATE"),
                         {"t": self.ta_id})
            thread = threading.Thread(target=demote)
            started = time.monotonic()
            thread.start()
            time.sleep(1.0)
            self.assertTrue(thread.is_alive(), "demotion should be waiting on the tenant lock")
            conn.execute(text("DELETE FROM memberships WHERE id = :i"), {"i": co_owner})
            txn.commit()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(result["done"] - started, 1.0)
        self.assertEqual(result["status"], 400, result)
        self.assertEqual(result["body"]["code"], "last_owner")
        self.assertEqual(self.role_of(self.ta_id, self.owner_a_id), "owner")


if __name__ == "__main__":
    unittest.main()
