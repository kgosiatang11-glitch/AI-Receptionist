"""Tests for smartdesk.services.knowledge.seed_sections.

Covers the exact gap found via live inspection of the SmartDesk tenant's
Supabase data: a knowledge section created blank (e.g. by an earlier
partial seed run, before the tenant's spec had content for it) stayed
blank forever, because the original logic only ever checked "does this
section exist yet" and never revisited a row once created.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import KNOWLEDGE_SECTIONS, KnowledgeDocument  # noqa: E402
from smartdesk.services.knowledge import seed_sections  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

SPEC = {
    "business_information": {"body": "We are a padel club."},
    "services": {"data": {"items": ["Court hire", "Coaching"]}},
    "pricing": {"body": "P150 per hour."},
}


class SeedSectionsTests(MultiTenantTestCase):
    def test_creates_every_section_on_a_fresh_tenant(self):
        seed_sections(self.padel, SPEC)
        db.session.commit()
        sections = {
            d.section: d
            for d in KnowledgeDocument.query.filter_by(tenant_id=self.padel.id).all()
        }
        self.assertEqual(set(sections), set(KNOWLEDGE_SECTIONS))

    def test_sections_with_spec_content_are_published(self):
        seed_sections(self.padel, SPEC)
        db.session.commit()
        business_info = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="business_information"
        ).one()
        self.assertTrue(business_info.is_published)
        self.assertEqual(business_info.body, "We are a padel club.")

    def test_a_data_only_section_is_published_even_with_no_body(self):
        """The exact bug found live: 'services' has only structured `data`,
        no `body` text, and should still count as published content."""
        seed_sections(self.padel, SPEC)
        db.session.commit()
        services = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="services"
        ).one()
        self.assertTrue(services.is_published)
        self.assertEqual(services.data, {"items": ["Court hire", "Coaching"]})

    def test_sections_with_no_spec_content_stay_blank_and_unpublished(self):
        seed_sections(self.padel, SPEC)
        db.session.commit()
        faqs = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="faqs"
        ).one()
        self.assertFalse(faqs.is_published)
        self.assertIsNone(faqs.body)

    def test_a_blank_existing_row_is_backfilled_on_a_later_call(self):
        """Reproduces the live bug exactly: a section row already exists,
        created blank (e.g. by an earlier seed run before the spec had
        content for it), and a later call with the real spec should fill
        it in rather than skipping it forever."""
        db.session.add(
            KnowledgeDocument(
                tenant_id=self.padel.id, section="services",
                title="Services", body=None, data={}, is_published=False,
            )
        )
        db.session.commit()

        seed_sections(self.padel, SPEC)
        db.session.commit()

        services = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="services"
        ).one()
        self.assertTrue(services.is_published)
        self.assertEqual(services.data, {"items": ["Court hire", "Coaching"]})

    def test_a_row_with_real_content_is_never_overwritten(self):
        """The critical safety property: a business owner's own edit must
        survive a re-seed, even if it doesn't match what the spec says."""
        db.session.add(
            KnowledgeDocument(
                tenant_id=self.padel.id, section="pricing",
                title="Pricing", body="Actually P200 per hour, we raised it.",
                data={}, is_published=True,
            )
        )
        db.session.commit()

        seed_sections(self.padel, SPEC)  # spec says "P150 per hour."
        db.session.commit()

        pricing = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="pricing"
        ).one()
        self.assertEqual(pricing.body, "Actually P200 per hour, we raised it.")

    def test_a_row_with_only_data_and_no_body_is_not_considered_blank(self):
        """A section with real structured data but no body text is NOT
        blank -- it must not be treated as backfillable and overwritten."""
        db.session.add(
            KnowledgeDocument(
                tenant_id=self.padel.id, section="services",
                title="Services", body=None,
                data={"items": ["The business's own real list"]},
                is_published=True,
            )
        )
        db.session.commit()

        seed_sections(self.padel, SPEC)
        db.session.commit()

        services = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="services"
        ).one()
        self.assertEqual(services.data, {"items": ["The business's own real list"]})

    def test_running_seed_sections_twice_is_idempotent(self):
        seed_sections(self.padel, SPEC)
        db.session.commit()
        seed_sections(self.padel, SPEC)
        db.session.commit()
        count = KnowledgeDocument.query.filter_by(tenant_id=self.padel.id).count()
        self.assertEqual(count, len(KNOWLEDGE_SECTIONS))

    def test_sections_are_isolated_per_tenant(self):
        seed_sections(self.padel, SPEC)
        db.session.commit()
        salon_services = KnowledgeDocument.query.filter_by(
            tenant_id=self.salon.id, section="services"
        ).one_or_none()
        self.assertIsNone(salon_services)

    def test_empty_spec_creates_only_blank_unpublished_sections(self):
        seed_sections(self.padel, {})
        db.session.commit()
        docs = KnowledgeDocument.query.filter_by(tenant_id=self.padel.id).all()
        self.assertTrue(all(not d.is_published for d in docs))


if __name__ == "__main__":
    unittest.main()