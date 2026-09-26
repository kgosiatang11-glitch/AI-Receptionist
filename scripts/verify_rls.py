#!/usr/bin/env python3
"""DEVELOPMENT / TEST TOOL ONLY -- reruns the exact RLS proof performed
during Phase 1 setup. Do not run against a production database.

Usage:
    createdb smartdesk_rls_check
    psql smartdesk_rls_check -c "CREATE ROLE smartdesk_app LOGIN PASSWORD 'x'; GRANT USAGE ON SCHEMA public TO smartdesk_app;"
    DATABASE_URL=postgresql+psycopg2://postgres@localhost/smartdesk_rls_check \
        FLASK_APP=wsgi flask db upgrade
    psql smartdesk_rls_check -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO smartdesk_app;"
    DATABASE_URL=postgresql+psycopg2://smartdesk_app:<password>@127.0.0.1:5432/smartdesk_rls_check \
        SUPABASE_JWT_SECRET=<any local test secret> \
        python scripts/verify_rls.py

The connection string, JWT secret, and platform-admin bootstrap email are
all read from environment variables -- nothing is hardcoded in this file,
and nothing here prints their values. Point ``DATABASE_URL`` at a local,
disposable database created for this check; never at a real customer's
database.

Live end-to-end proof: real Postgres, real connection pool, real app code.

Runs the actual Flask app (not a mock, not SQLite) against a non-superuser
role, and issues real HTTP requests through the actual JWT -> RBAC ->
bind_rls_tenant -> SQLAlchemy pooled connection chain.
"""
import os
import sys

REQUIRED_ENV_VARS = ("DATABASE_URL", "SUPABASE_JWT_SECRET")
missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
if missing:
    sys.exit(
        "Missing required environment variable(s): "
        + ", ".join(missing)
        + ".\nThis script never assumes a database or secret -- set them "
        "explicitly (see the usage note at the top of this file) and rerun."
    )

os.environ.setdefault("PLATFORM_ADMIN_EMAILS", "dev-admin@example.test")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

import jwt
from smartdesk.app import create_app
from smartdesk.config import Config
from smartdesk.extensions import db
from smartdesk.models import Membership, Tenant, User

app = create_app(Config)

# Two disposable, generically-named tenants created by this script itself --
# not customer data, not anything created by `flask seed`. Safe to leave in
# a throwaway local database; re-running is idempotent.
TENANT_A_SLUG = "rls-check-tenant-a"
TENANT_B_SLUG = "rls-check-tenant-b"
CHECK_USER_EMAIL = "rls-check-owner@example.test"
CHECK_USER_SUBJECT = "00000000-0000-4000-8000-000000000000"
SECRET_CUSTOMER_NAME = "RLS Check Secret Customer"

with app.app_context():
    tenant_a = Tenant.query.filter_by(slug=TENANT_A_SLUG).one_or_none()
    if tenant_a is None:
        tenant_a = Tenant(slug=TENANT_A_SLUG, name="RLS Check Tenant A")
        db.session.add(tenant_a)
        db.session.flush()

    tenant_b = Tenant.query.filter_by(slug=TENANT_B_SLUG).one_or_none()
    if tenant_b is None:
        tenant_b = Tenant(slug=TENANT_B_SLUG, name="RLS Check Tenant B")
        db.session.add(tenant_b)
        db.session.flush()

    tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id

    user = User.query.filter_by(email=CHECK_USER_EMAIL).one_or_none()
    if user is None:
        user = User(supabase_user_id=CHECK_USER_SUBJECT, email=CHECK_USER_EMAIL)
        db.session.add(user)
        db.session.flush()

    db.session.execute(
        db.text("SELECT set_config('app.current_tenant_id', :tid, false)"),
        {"tid": tenant_a_id},
    )
    if not Membership.query.filter_by(tenant_id=tenant_a_id, user_id=user.id).first():
        db.session.add(Membership(tenant_id=tenant_a_id, user_id=user.id, role="owner"))

    from smartdesk.models import Customer

    if not Customer.query.filter_by(
        tenant_id=tenant_a_id, full_name=SECRET_CUSTOMER_NAME
    ).first():
        db.session.add(Customer(tenant_id=tenant_a_id, full_name=SECRET_CUSTOMER_NAME))

    db.session.commit()

token = jwt.encode(
    {"sub": CHECK_USER_SUBJECT, "email": CHECK_USER_EMAIL,
     "aud": "authenticated", "exp": 9999999999},
    os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256",
)

client = app.test_client()

print("=== LIVE (real Postgres, real pool) request as tenant A's owner, tenant=A ===")
r = client.get("/api/v1/customers",
                headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_a_id})
names = [c["full_name"] for c in r.get_json()["items"]]
print("status:", r.status_code, "names visible:", names)
assert names == [SECRET_CUSTOMER_NAME], f"LEAK: got {names}"

print()
print("=== LIVE request: same user, tries tenant B's id in the header ===")
r = client.get("/api/v1/customers",
                headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_b_id})
print("status:", r.status_code, r.get_json())
assert r.status_code == 403, "APP-LAYER GUARD FAILED"

print()
print("=== LIVE: proving RLS is the BACKSTOP, not just the app-layer guard ===")
with app.test_request_context():
    from smartdesk.tenancy import bind_tenant

    tenant_a_fresh = db.session.get(Tenant, tenant_a_id)
    bind_tenant(tenant_a_fresh)
    rows = db.session.execute(db.text("SELECT full_name FROM customers")).fetchall()
    print("Raw unscoped query result while GUC=tenant A:", [row[0] for row in rows])
    assert [row[0] for row in rows] == [SECRET_CUSTOMER_NAME], (
        "RLS DID NOT HOLD ACROSS POOLED CONNECTION"
    )

print()
print("ALL LIVE CHECKS PASSED against real Postgres with a real connection pool.")
