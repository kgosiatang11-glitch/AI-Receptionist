#!/usr/bin/env python3
"""Reruns the exact RLS proof performed during Phase 1 setup.

Usage:
    createdb smartdesk_rls_check
    psql smartdesk_rls_check -c "CREATE ROLE smartdesk_app LOGIN PASSWORD 'x'; GRANT USAGE ON SCHEMA public TO smartdesk_app;"
    DATABASE_URL=postgresql+psycopg2://postgres@localhost/smartdesk_rls_check \
        FLASK_APP=wsgi flask db upgrade
    psql smartdesk_rls_check -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smartdesk_app;"
    python scripts/verify_rls.py

Edit the DATABASE_URL near the top of this file to point at your own
smartdesk_app role and database before running.

Live end-to-end proof: real Postgres, real connection pool, real app code.

Runs the actual Flask app (not a mock, not SQLite) against a non-superuser
role, and issues real HTTP requests through the actual JWT -> RBAC ->
bind_rls_tenant -> SQLAlchemy pooled connection chain.
"""
import os
os.environ["DATABASE_URL"] = "postgresql+psycopg2://smartdesk_app:smartdesk_test_pw@127.0.0.1:5432/smartdesk_test"
os.environ["SUPABASE_JWT_SECRET"] = "test-secret"
os.environ["PLATFORM_ADMIN_EMAILS"] = "admin@smartdesk.ai"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"

import jwt
from smartdesk.app import create_app
from smartdesk.config import Config
from smartdesk.extensions import db
from smartdesk.models import Membership, Tenant, User

app = create_app(Config)

with app.app_context():
    padel = Tenant.query.filter_by(slug="10by20").one()
    salon = Tenant.query.filter_by(slug="salon").one()
    padel_id, salon_id = padel.id, salon.id

    user = User.query.filter_by(email="padel-owner@test.com").one_or_none()
    if user is None:
        user = User(
            supabase_user_id="33333333-1111-4444-8888-000000000001",
            email="padel-owner@test.com",
        )
        db.session.add(user)
        db.session.flush()

    db.session.execute(
        db.text("SELECT set_config('app.current_tenant_id', :tid, false)"),
        {"tid": padel_id},
    )
    if not Membership.query.filter_by(tenant_id=padel_id, user_id=user.id).first():
        db.session.add(Membership(tenant_id=padel_id, user_id=user.id, role="owner"))
    db.session.commit()

token = jwt.encode(
    {"sub": "33333333-1111-4444-8888-000000000001", "email": "padel-owner@test.com",
     "aud": "authenticated", "exp": 9999999999},
    "test-secret", algorithm="HS256",
)

client = app.test_client()

print("=== LIVE (real Postgres, real pool) request as 10by20's owner, tenant=10by20 ===")
r = client.get("/api/v1/customers",
                headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": padel_id})
names = [c["full_name"] for c in r.get_json()["items"]]
print("status:", r.status_code, "names visible:", names)
assert names == ["Padel Secret Customer"], f"LEAK: got {names}"

print()
print("=== LIVE request: same user, tries salon's tenant id in the header ===")
r = client.get("/api/v1/customers",
                headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": salon_id})
print("status:", r.status_code, r.get_json())
assert r.status_code == 403, "APP-LAYER GUARD FAILED"

print()
print("=== LIVE: proving RLS is the BACKSTOP, not just the app-layer guard ===")
with app.test_request_context():
    from smartdesk.tenancy import bind_tenant

    padel_fresh = db.session.get(Tenant, padel_id)
    bind_tenant(padel_fresh)
    rows = db.session.execute(db.text("SELECT full_name FROM customers")).fetchall()
    print("Raw unscoped query result while GUC=10by20:", [row[0] for row in rows])
    assert [row[0] for row in rows] == ["Padel Secret Customer"], (
        "RLS DID NOT HOLD ACROSS POOLED CONNECTION"
    )

print()
print("ALL LIVE CHECKS PASSED against real Postgres with a real connection pool.")
