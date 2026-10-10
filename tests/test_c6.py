"""C6 -- user assignment & membership hardening, SQLite run.

Assertions live in ``tests/c6_checks.py`` and are shared with the PostgreSQL run.
"""

from __future__ import annotations

from smartdesk.extensions import db
from smartdesk.models import Conversation, Lead, _assignee_fk_ondelete
from tests.c6_checks import C6Checks
from tests.test_multitenant import MultiTenantTestCase


class C6Tests(C6Checks, MultiTenantTestCase):
    def _build_fixtures(self) -> None:
        super()._build_fixtures()
        self.ta, self.tb = self.padel, self.salon
        self.build_c5_world()
        self.build_c6_world()


class C6BootstrapTests(MultiTenantTestCase):
    """An allow-listed e-mail only becomes platform admin once verified."""

    def test_unverified_allow_listed_email_cannot_bootstrap(self):
        from smartdesk.models import User
        from smartdesk.security import rbac
        from smartdesk.security.jwt_auth import TokenClaims

        email = "bootstrap@smartdesk.ai"
        claims = TokenClaims(subject="00000000-0000-4000-8000-0000000000aa",
                             email=email, raw={})
        self.app.config["PLATFORM_ADMIN_EMAILS"] = (email,)
        user = rbac._sync_user(claims, email_verified=False)
        db.session.commit()
        self.assertFalse(user.is_platform_admin)
        user = rbac._sync_user(claims, email_verified=True)
        db.session.commit()
        self.assertTrue(db.session.get(User, user.id).is_platform_admin)


class ModelListenerTests(MultiTenantTestCase):
    def test_assignee_fk_ondelete_is_dialect_specific(self):
        from types import SimpleNamespace

        def conn(name):
            return SimpleNamespace(dialect=SimpleNamespace(name=name))

        for table in (Lead.__table__, Conversation.__table__):
            fk = next(c for c in table.foreign_key_constraints
                      if c.name and c.name.endswith("assignee_member"))
            original = fk.ondelete
            try:
                _assignee_fk_ondelete(table, conn("postgresql"))
                self.assertEqual(fk.ondelete, "SET NULL (assigned_user_id)")
                _assignee_fk_ondelete(table, conn("sqlite"))
                self.assertEqual(fk.ondelete, "SET NULL")
            finally:
                fk.ondelete = original
