"""Manual WhatsApp provisioning rules shared by API endpoints.

No call in this module contacts Twilio or Meta.  A provider check is a
documented administrator attestation until a supported provider integration is
approved and added.
"""
from __future__ import annotations

from datetime import datetime, timezone

from smartdesk.extensions import db
from smartdesk.models import Channel, WhatsAppOnboarding
from smartdesk.tenancy import normalize_address

VERIFICATION_STATUSES = ("not_applicable", "pending", "confirmed", "failed", "unverified")
REGISTRATION_STATUSES = ("unverified", "pending", "registered", "failed")
ADMIN_STATUSES = ("under_review", "provisioning", "awaiting_verification", "configuration_pending", "ready_for_testing", "failed", "cancelled")
NUMBER_CHANGEABLE_STATUSES = ("requested", "failed", "cancelled", "suspended")
TRANSITIONS = {
    "requested": {"under_review", "cancelled"},
    "under_review": {"provisioning", "awaiting_verification", "configuration_pending", "failed", "cancelled"},
    "provisioning": {"awaiting_verification", "configuration_pending", "ready_for_testing", "failed", "cancelled"},
    "awaiting_verification": {"provisioning", "configuration_pending", "ready_for_testing", "failed", "cancelled"},
    "configuration_pending": {"provisioning", "awaiting_verification", "ready_for_testing", "failed", "cancelled"},
    "ready_for_testing": {"provisioning", "failed", "cancelled"},
    "failed": {"under_review", "provisioning", "cancelled"},
    "cancelled": {"under_review"},
    "active": {"suspended"},
    "suspended": {"under_review", "cancelled"},
}


def submit_request(tenant_id: str, raw_number: str, display_name: str | None):
    number = normalize_address(raw_number)
    if not number or len(number) < 8 or len(number) > 16:
        raise ValueError("Enter a valid WhatsApp number in international format")
    onboarding = WhatsAppOnboarding.query.filter_by(tenant_id=tenant_id).one_or_none()
    if onboarding is None:
        onboarding = WhatsAppOnboarding(tenant_id=tenant_id, requested_number=number, display_name=display_name or None)
        db.session.add(onboarding)
    elif onboarding.requested_number == number:
        # A retry must never erase evidence or restart a workflow.
        return onboarding, False
    elif onboarding.status not in NUMBER_CHANGEABLE_STATUSES:
        raise ValueError("WhatsApp setup is already in progress; contact SmartDesk support to change the number")
    else:
        onboarding.requested_number = number
        onboarding.display_name = display_name or None
        onboarding.status = "requested"
        # Every remaining value documents a particular sender/number and is
        # therefore invalid for the replacement number.
        onboarding.twilio_sender_sid = None
        onboarding.twilio_configuration_recorded = False
        onboarding.phone_verification_status = "not_applicable"
        onboarding.meta_verification_status = "not_applicable"
        onboarding.registration_status = "unverified"
        onboarding.outstanding_requirements = []
        onboarding.last_provider_check_at = None
        onboarding.last_provider_check_passed = None
        onboarding.provider_check_source = None
        onboarding.receptionist_active = False
        onboarding.provisioning_error = None
    return onboarding, True


def activation_error(onboarding: WhatsAppOnboarding) -> str | None:
    if not onboarding.twilio_configuration_recorded:
        return "Twilio configuration has not been recorded"
    if onboarding.registration_status != "registered":
        return "WhatsApp sender registration is not provider-confirmed"
    if onboarding.phone_verification_status not in ("confirmed", "not_applicable"):
        return "Phone-number verification is incomplete"
    if onboarding.meta_verification_status not in ("confirmed", "not_applicable"):
        return "Meta verification requirements are incomplete"
    if onboarding.outstanding_requirements:
        return "Outstanding onboarding requirements remain"
    if onboarding.last_provider_check_passed is not True:
        return "A successful connection check or explicit administrator confirmation is required"
    return None


def activate(onboarding: WhatsAppOnboarding):
    if onboarding.status == "active":
        channel = Channel.query.filter_by(
            tenant_id=onboarding.tenant_id,
            kind="whatsapp",
            address=onboarding.requested_number,
        ).one_or_none()
        if channel is None or not channel.is_active:
            raise ValueError("Active onboarding has no active WhatsApp channel")
        return channel
    if onboarding.status != "ready_for_testing":
        raise ValueError("WhatsApp onboarding must be ready for testing before activation")
    error = activation_error(onboarding)
    if error:
        raise ValueError(error)
    channel = Channel.query.filter_by(kind="whatsapp", address=onboarding.requested_number).one_or_none()
    if channel is not None and channel.tenant_id != onboarding.tenant_id:
        raise ValueError("That WhatsApp number is already mapped to another tenant")
    if channel is None:
        channel = Channel(tenant_id=onboarding.tenant_id, kind="whatsapp", address=onboarding.requested_number, display_name=onboarding.display_name, is_active=True)
        db.session.add(channel)
    else:
        channel.is_active = True
        channel.display_name = onboarding.display_name or channel.display_name
    # ReceptionistProfile.is_active is a tenant-wide receptionist setting,
    # not a WhatsApp provisioning switch.  Changing it here would re-enable
    # deliberately disabled services and couple WhatsApp to voice.
    onboarding.status = "active"
    onboarding.receptionist_active = True
    return channel


def suspend(onboarding: WhatsAppOnboarding):
    if onboarding.status == "suspended":
        return
    if onboarding.status != "active":
        raise ValueError("Only an active WhatsApp onboarding can be suspended")
    channel = Channel.query.filter_by(tenant_id=onboarding.tenant_id, kind="whatsapp", address=onboarding.requested_number).one_or_none()
    if channel:
        channel.is_active = False
    # Channel resolution already rejects inactive channels.  Do not mutate the
    # shared profile: voice and other tenant facilities must remain unchanged.
    transition(onboarding, "suspended")
    onboarding.receptionist_active = False


def mark_provider_check(onboarding: WhatsAppOnboarding, passed: bool, source: str):
    onboarding.last_provider_check_at = datetime.now(timezone.utc)
    onboarding.last_provider_check_passed = passed
    onboarding.provider_check_source = source


def transition(onboarding: WhatsAppOnboarding, target: str) -> None:
    if target not in TRANSITIONS.get(onboarding.status, set()):
        raise ValueError("Invalid onboarding status transition")
    onboarding.status = target
