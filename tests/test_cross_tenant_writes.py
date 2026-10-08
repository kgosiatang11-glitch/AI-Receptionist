"""C5 -- cross-tenant write isolation, SQLite run.

The assertions live in ``tests/cross_tenant_checks.py`` and are shared with the
PostgreSQL run (``test_cross_tenant_writes_postgres.py``).
"""

from __future__ import annotations

import unittest

from smartdesk.extensions import db
from smartdesk.services.ownership import (
    ReferenceNotFound,
    normalize_reference_id,
    optional_owned,
    require_owned,
    require_tenant_user,
)
from tests.cross_tenant_checks import C5Fixtures, CrossTenantWriteChecks
from tests.test_multitenant import MultiTenantTestCase


class CrossTenantWriteTests(CrossTenantWriteChecks, MultiTenantTestCase):
    def _build_fixtures(self) -> None:
        super()._build_fixtures()
        self.ta, self.tb = self.padel, self.salon
        self.build_c5_world()


class OwnershipHelperTests(C5Fixtures, MultiTenantTestCase):
    """The helper on its own: tenant filter is in the query, not after it."""

    def _build_fixtures(self) -> None:
        super()._build_fixtures()
        self.ta, self.tb = self.padel, self.salon
        self.build_c5_world()

    def test_require_owned_returns_same_tenant_row_only(self):
        from smartdesk.models import Customer

        row = require_owned(Customer, self.cust_a_id, self.ta_id, "customer_id")
        self.assertEqual(row.id, self.cust_a_id)
        with self.assertRaises(ReferenceNotFound):
            require_owned(Customer, self.cust_b_id, self.ta_id, "customer_id")

    def test_other_tenants_row_is_never_loaded_into_the_session(self):
        from smartdesk.models import Customer

        db.session.remove()
        with self.assertRaises(ReferenceNotFound):
            require_owned(Customer, self.cust_b_id, self.ta_id, "customer_id")
        self.assertNotIn(Customer, {type(o) for o in db.session.identity_map.values()})

    def test_optional_owned_treats_none_and_empty_as_no_reference(self):
        from smartdesk.models import Customer

        self.assertIsNone(optional_owned(Customer, None, self.ta_id, "customer_id"))
        self.assertIsNone(optional_owned(Customer, "", self.ta_id, "customer_id"))

    def test_require_tenant_user_is_membership_based(self):
        self.assertEqual(
            require_tenant_user(self.member_a_id, self.ta_id).id, self.member_a_id)
        for outsider in (self.owner_b_id, self.admin_id):
            with self.assertRaises(ReferenceNotFound):
                require_tenant_user(outsider, self.ta_id)

    def test_non_tenant_scoped_model_is_a_programming_error(self):
        from smartdesk.models import User

        with self.assertRaises(TypeError):
            require_owned(User, self.owner_a_id, self.ta_id, "user_id")

    def test_normalize_canonicalises_valid_and_rejects_everything_else(self):
        import uuid

        value = str(uuid.uuid4())
        self.assertEqual(normalize_reference_id(f"  {value.upper()} ", "f"), value)
        for bad in (None, 1, "", "x", b"bytes", object()):
            with self.assertRaises(ReferenceNotFound):
                normalize_reference_id(bad, "f")


if __name__ == "__main__":
    unittest.main()
