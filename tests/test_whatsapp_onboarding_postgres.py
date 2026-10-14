"""PostgreSQL RLS proof for the WhatsApp onboarding table."""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from smartdesk.extensions import db
from smartdesk.models import Tenant
from tests.test_cross_tenant_writes_postgres import RlsProbeTests


class WhatsAppOnboardingRlsTests(RlsProbeTests):
    def test_onboarding_table_has_forced_tenant_rls_policy(self):
        row = db.session.execute(text("""
            SELECT c.relforcerowsecurity, p.polname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_policy p ON p.polrelid = c.oid
            WHERE n.nspname = 'public' AND c.relname = 'whatsapp_onboardings'
              AND p.polname = 'whatsapp_onboardings_tenant_isolation'
        """)).one()
        self.assertTrue(row[0])

    def test_rls_hides_and_refuses_other_tenant_onboarding_rows(self):
        other = Tenant(slug="onboarding-other", name="Other")
        db.session.add(other); db.session.flush()
        db.session.execute(text("""
            INSERT INTO whatsapp_onboardings
            (id, tenant_id, requested_number, status, twilio_configuration_recorded,
             phone_verification_status, meta_verification_status, registration_status,
             outstanding_requirements, receptionist_active, created_at, updated_at)
            VALUES (:id, :tenant, '+26779999999', 'requested', false,
                    'not_applicable', 'not_applicable', 'unverified', '[]'::jsonb,
                    false, now(), now())
        """), {"id": str(uuid.uuid4()), "tenant": other.id})
        db.session.commit()
        with db.engine.begin() as conn:
            self._as_tenant(conn, self.ta_id)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM whatsapp_onboardings")).scalar(), 0)
        with self.assertRaises(DBAPIError):
            with db.engine.begin() as conn:
                self._as_tenant(conn, self.ta_id)
                conn.execute(text("""INSERT INTO whatsapp_onboardings
                    (id, tenant_id, requested_number, status, twilio_configuration_recorded,
                     phone_verification_status, meta_verification_status, registration_status,
                     outstanding_requirements, receptionist_active, created_at, updated_at)
                    VALUES (:id, :tenant, '+26778888888', 'requested', false,
                            'not_applicable', 'not_applicable', 'unverified', '[]'::jsonb,
                            false, now(), now())"""),
                    {"id": str(uuid.uuid4()), "tenant": other.id})
