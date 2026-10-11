"""The platform always keeps one ELIGIBLE platform administrator (C6 follow-up).

Eligible = ``is_platform_admin`` AND ``is_active`` AND a confirmed email (see
``smartdesk/services/platform_admins.py``).

A sequential caller can never hit the "last administrator" rule on its own: the
caller is itself an eligible administrator and cannot target itself.  The rule
exists for the *concurrent* case -- another request changed the administrator
set while this one waited for the platform lock.  SQLite cannot run two
transactions at once, so these tests simulate exactly that: the lock hook
commits a competing change, which is what a request that lost the race would
find when it finally got the lock.  The same scenarios run with real concurrent
transactions in ``test_platform_admin_invariant_postgres.py``.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import sqlalchemy as sa

from smartdesk.extensions import db
from smartdesk.models import AuditLog, Membership, User
from smartdesk.services import platform_admins
from smartdesk.services.platform_admins import LastPlatformAdminError
from tests.test_multitenant import MultiTenantTestCase

CONFIRMED = "smartdesk.security.rbac._is_email_confirmed"
LOCK = "smartdesk.services.platform_admins.lock_platform_admins"


class AdminInvariantCase(MultiTenantTestCase):
    """``self.admin_user`` (A, the caller) plus extra administrators B and C."""

    def setUp(self) -> None:
        super().setUp()
        self.admin_b = User(supabase_user_id="sub-admin-b", email="b@smartdesk.ai",
                            is_platform_admin=True)
        self.admin_c = User(supabase_user_id="sub-admin-c", email="c@smartdesk.ai",
                            is_platform_admin=True)
        db.session.add_all([self.admin_b, self.admin_c])
        db.session.commit()
        self.a_id, self.b_id, self.c_id = (
            self.admin_user.id, self.admin_b.id, self.admin_c.id)

    # -- helpers ---------------------------------------------------------
    def patch_user(self, actor, target_id, **payload):
        return self.client.patch(
            f"/api/v1/admin/users/{target_id}", headers=self.auth(actor), json=payload)

    def flags(self, user_id):
        db.session.expire_all()
        user = db.session.get(User, user_id)
        return user.is_platform_admin, user.is_active

    def audit_count(self):
        return AuditLog.query.filter_by(action="user.update").count()

    def lose_the_race(self, user_id, **values):
        """While the request waits for the platform lock, another request commits ``values``."""
        def competing_commit():
            db.session.execute(sa.update(User).where(User.id == user_id).values(**values))
            db.session.commit()
        return patch(LOCK, side_effect=competing_commit)

    def remove_everyone_but(self, *keep_ids):
        """Make only ``keep_ids`` platform administrators (committed)."""
        User.query.filter(User.id.notin_(keep_ids)).update(
            {"is_platform_admin": False}, synchronize_session=False)
        db.session.commit()


class MultipleAdministratorTests(AdminInvariantCase):
    def test_demoting_one_of_several_administrators_is_allowed(self):
        response = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.flags(self.b_id), (False, True))
        self.assertEqual(self.flags(self.a_id), (True, True))
        self.assertEqual(self.audit_count(), 1)

    def test_deactivating_one_of_several_administrators_is_allowed(self):
        response = self.patch_user(self.admin_user, self.b_id, is_active=False)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.flags(self.b_id), (True, False))

    def test_the_sole_remaining_administrator_cannot_be_removed_by_anyone(self):
        self.assertEqual(
            self.patch_user(self.admin_user, self.b_id, is_platform_admin=False).status_code, 200)
        self.assertEqual(
            self.patch_user(self.admin_user, self.c_id, is_active=False).status_code, 200)
        # A is now the only eligible administrator.  A cannot remove itself ...
        for payload in ({"is_platform_admin": False}, {"is_active": False}):
            r = self.patch_user(self.admin_user, self.a_id, **payload)
            self.assertEqual(r.status_code, 400, r.get_json())
        # ... and the others no longer hold the right to try.
        for demoted in (self.admin_b,):
            r = self.patch_user(demoted, self.a_id, is_platform_admin=False)
            self.assertEqual(r.status_code, 403, r.get_json())
        self.assertEqual(self.flags(self.a_id), (True, True))

    def test_promoting_and_activating_never_hit_the_rule(self):
        self.remove_everyone_but(self.a_id)
        self.assertEqual(self.patch_user(self.admin_user, self.b_id, is_platform_admin=True).status_code, 200)
        self.assertEqual(self.patch_user(self.admin_user, self.b_id, is_active=True).status_code, 200)
        # Re-asserting the current state is also fine.
        self.assertEqual(self.patch_user(self.admin_user, self.a_id, is_platform_admin=True).status_code, 200)


class LastAdministratorTests(AdminInvariantCase):
    """The request that lost the race must be refused and must change nothing."""

    def assertRefused(self, response, target_id, expected_flags=(True, True)):
        self.assertEqual(response.status_code, 400, response.get_json())
        body = response.get_json()
        self.assertEqual(body["code"], "last_platform_admin")
        self.assertIn("last", body["error"].lower())
        self.assertEqual(self.flags(target_id), expected_flags)
        self.assertEqual(self.audit_count(), 0, "a refused change must not be audited as applied")

    def test_cannot_demote_the_last_administrator(self):
        self.remove_everyone_but(self.a_id, self.b_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertRefused(r, self.b_id)
        self.assertEqual(self.flags(self.a_id), (False, True), "the competing change stands")

    def test_cannot_deactivate_the_last_administrator(self):
        self.remove_everyone_but(self.a_id, self.b_id)
        with self.lose_the_race(self.a_id, is_active=False):
            r = self.patch_user(self.admin_user, self.b_id, is_active=False)
        self.assertRefused(r, self.b_id)

    def test_demote_and_deactivate_in_one_request_is_refused_as_a_unit(self):
        self.remove_everyone_but(self.a_id, self.b_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id,
                                is_platform_admin=False, is_active=False)
        self.assertRefused(r, self.b_id)

    def test_string_false_is_parsed_strictly_and_still_guarded(self):
        self.remove_everyone_but(self.a_id, self.b_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id, is_platform_admin="false")
        self.assertRefused(r, self.b_id)

    def test_other_administrators_who_are_not_active_do_not_count(self):
        # C is an administrator but deactivated: not usable, so B is the last.
        self.admin_c.is_active = False
        db.session.commit()
        self.remove_everyone_but(self.a_id, self.b_id, self.c_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertRefused(r, self.b_id)

    def test_other_administrators_with_an_unconfirmed_email_do_not_count(self):
        self.remove_everyone_but(self.a_id, self.b_id, self.c_id)
        with patch(CONFIRMED, side_effect=lambda claims: claims.subject != "sub-admin-c"):
            with self.lose_the_race(self.a_id, is_platform_admin=False):
                r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertRefused(r, self.b_id)

    def test_a_confirmed_active_administrator_is_a_valid_survivor(self):
        """Control for the two tests above: C is eligible, so B may be demoted."""
        self.remove_everyone_but(self.a_id, self.b_id, self.c_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.flags(self.b_id), (False, True))
        self.assertEqual(self.flags(self.c_id), (True, True))

    def test_a_tenant_owner_who_is_not_a_platform_admin_does_not_count(self):
        self.remove_everyone_but(self.a_id, self.b_id)   # padel_user is a tenant OWNER only
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
        self.assertRefused(r, self.b_id)

    def test_removing_someone_who_never_counted_cannot_reduce_the_set(self):
        """Demoting an already-deactivated administrator takes nothing away."""
        self.admin_c.is_active = False
        db.session.commit()
        self.remove_everyone_but(self.a_id, self.c_id)
        with self.lose_the_race(self.a_id, is_platform_admin=False):
            r = self.patch_user(self.admin_user, self.c_id, is_platform_admin=False)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.flags(self.c_id), (False, False))


class ExistingProtectionsPreservedTests(AdminInvariantCase):
    def test_self_protection_is_decided_before_anything_is_locked(self):
        with patch(LOCK) as lock:
            r1 = self.patch_user(self.admin_user, self.a_id, is_platform_admin=False)
            r2 = self.patch_user(self.admin_user, self.a_id, is_active=False)
            r3 = self.patch_user(self.admin_user, self.a_id,
                                 is_platform_admin=True, is_active=False)
        self.assertEqual([r1.status_code, r2.status_code, r3.status_code], [400, 400, 400])
        self.assertIn("own platform administrator access", r1.get_json()["error"])
        self.assertIn("own account", r2.get_json()["error"])
        self.assertIn("own account", r3.get_json()["error"])
        lock.assert_not_called()
        self.assertEqual(self.flags(self.a_id), (True, True))

    def test_invalid_booleans_are_rejected_before_anything_is_locked(self):
        for bad in ("maybe", 0, 1, None, [], "yes"):
            with self.subTest(value=bad), patch(LOCK) as lock:
                r = self.patch_user(self.admin_user, self.b_id, is_platform_admin=bad)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.get_json()["field"], "is_platform_admin")
                lock.assert_not_called()
        self.assertEqual(self.flags(self.b_id), (True, True))

    def test_only_reducing_changes_take_the_platform_lock(self):
        with patch(LOCK) as lock:
            self.patch_user(self.admin_user, self.padel_user.id, is_platform_admin=True)
            self.patch_user(self.admin_user, self.padel_user.id, is_active=True)
            lock.assert_not_called()
            self.patch_user(self.admin_user, self.b_id, is_platform_admin=False)
            self.assertEqual(lock.call_count, 1)
            self.patch_user(self.admin_user, self.c_id, is_active=False)
            self.assertEqual(lock.call_count, 2)

    def test_empty_body_is_still_nothing_to_update(self):
        r = self.patch_user(self.admin_user, self.b_id)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "Nothing to update")

    def test_last_owner_rule_still_applies_and_rolls_the_whole_request_back(self):
        """An administrator who is also the sole owner of a tenant: the demotion
        half of the request must NOT persist when the deactivation half is refused."""
        db.session.add(Membership(tenant_id=self.smartdesk.id, user_id=self.b_id, role="owner"))
        db.session.commit()
        r = self.patch_user(self.admin_user, self.b_id,
                            is_platform_admin=False, is_active=False)
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(r.get_json()["code"], "last_owner")
        self.assertEqual(self.flags(self.b_id), (True, True))
        self.assertEqual(self.audit_count(), 0)

    def test_successful_combined_change_is_applied_and_audited_once(self):
        r = self.patch_user(self.admin_user, self.b_id,
                            is_platform_admin=False, is_active=False)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.flags(self.b_id), (False, False))
        entries = AuditLog.query.filter_by(action="user.update").all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].meta, {"is_platform_admin": False, "is_active": False})
        self.assertEqual(entries[0].object_id, self.b_id)
        self.assertEqual(entries[0].actor_user_id, self.a_id)

    def test_a_non_admin_still_cannot_use_the_endpoint(self):
        r = self.patch_user(self.padel_user, self.b_id, is_platform_admin=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.flags(self.b_id), (True, True))


class ServiceTests(AdminInvariantCase):
    def test_other_eligible_admin_exists_excludes_the_given_user(self):
        self.remove_everyone_but(self.a_id)
        self.assertFalse(platform_admins.other_eligible_admin_exists(self.a_id))
        self.assertTrue(platform_admins.other_eligible_admin_exists(self.b_id))

    def test_inactive_and_unconfirmed_administrators_are_not_eligible(self):
        self.remove_everyone_but(self.a_id, self.b_id, self.c_id)
        self.admin_b.is_active = False
        db.session.commit()
        with patch(CONFIRMED, side_effect=lambda claims: claims.subject != "sub-admin-c"):
            self.assertFalse(platform_admins.other_eligible_admin_exists(self.a_id))
        self.assertTrue(platform_admins.other_eligible_admin_exists(self.a_id))   # C confirmed again

    def test_confirmation_lookup_failure_fails_closed(self):
        self.remove_everyone_but(self.a_id, self.b_id)
        with patch(CONFIRMED, return_value=False):
            self.assertFalse(platform_admins.other_eligible_admin_exists(self.a_id))

    def test_ensure_admin_remains_raises_only_when_the_set_would_empty(self):
        self.remove_everyone_but(self.b_id)
        with self.assertRaises(LastPlatformAdminError):
            platform_admins.ensure_admin_remains(self.admin_b, new_admin=False, new_active=None)
        with self.assertRaises(LastPlatformAdminError):
            platform_admins.ensure_admin_remains(self.admin_b, new_admin=None, new_active=False)
        # Not reducing: nothing changes, or the target is promoted/activated.
        platform_admins.ensure_admin_remains(self.admin_b, new_admin=None, new_active=None)
        platform_admins.ensure_admin_remains(self.admin_b, new_admin=True, new_active=True)

    def test_lock_is_a_harmless_no_op_without_postgresql(self):
        platform_admins.lock_platform_admins()
        platform_admins.begin_exclusive_change()

    def test_lock_key_is_a_stable_signed_64_bit_value(self):
        key = platform_admins.PLATFORM_ADMIN_LOCK_KEY
        self.assertIsInstance(key, int)
        self.assertGreaterEqual(key, -(2 ** 63))
        self.assertLess(key, 2 ** 63)


if __name__ == "__main__":
    unittest.main()
