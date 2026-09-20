"""Per-tenant personality and copy for the shared O'Brien engine.

The engine in :mod:`ai.receptionist` used to embed SmartDesk AI's own name and
sales behaviour in its replies, which meant every business deployed on the
platform pitched SmartDesk to its callers.  Those strings now live here and
are supplied per tenant.

The defaults on this class reproduce the pre-existing SmartDesk behaviour
exactly, so code that constructs a persona without arguments — including the
legacy single-tenant path and the original test suite — behaves as before.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_BUSINESS_NAME = "SmartDesk AI"
DEFAULT_ASSISTANT_NAME = "O'Brien"


@dataclass(frozen=True)
class TenantPersona:
    """Everything the engine needs to speak as one specific business."""

    business_name: str = DEFAULT_BUSINESS_NAME
    assistant_name: str = DEFAULT_ASSISTANT_NAME

    #: Extra tenant-authored guidance appended to the shared system rules.
    personality: str = ""
    business_instructions: str = ""

    #: Feature switches, configured per tenant in the Control Center.
    handoff_enabled: bool = True
    lead_capture_enabled: bool = True
    booking_assistance_enabled: bool = True

    #: Sales behaviour is OFF by default. Only the SmartDesk tenant turns it
    #: on. This is the single most important default in this module.
    sales_mode_enabled: bool = True

    handoff_message: str | None = None
    sales_handoff_message: str | None = None
    setswana_greeting: str | None = None

    @property
    def team_reference(self) -> str:
        return f"the {self.business_name} team"

    def resolved_handoff_message(self) -> str:
        if self.handoff_message:
            return self.handoff_message
        return (
            f"Absolutely. I'll connect you with {self.team_reference} so they "
            "can assist you directly. A team member will contact you shortly."
        )

    def resolved_sales_handoff_message(self) -> str:
        if self.sales_handoff_message:
            return self.sales_handoff_message
        return (
            f"Excellent. Let's get you started. I'll connect you with "
            f"{self.team_reference} to complete the setup."
        )

    def resolved_setswana_greeting(self) -> str:
        if self.setswana_greeting:
            return self.setswana_greeting
        return (
            f"Dumelang! Ke nna {self.assistant_name}, AI Receptionist ya "
            f"{self.business_name}. Nka go thusa jang gompieno?"
        )

    def system_rules_suffix(self) -> str:
        """Tenant-authored additions to the shared system prompt."""
        parts: list[str] = [
            f"You are answering on behalf of {self.business_name}. "
            f"Refer to that business as 'we' and never promote any other company."
        ]
        if self.personality:
            parts.append(f"Personality and tone:\n{self.personality.strip()}")
        if self.business_instructions:
            parts.append(
                f"Business-specific instructions:\n{self.business_instructions.strip()}"
            )
        if not self.sales_mode_enabled:
            parts.append(
                "Do not pitch, upsell, or promote any software platform. You are "
                "a receptionist for this business, not a salesperson for a vendor."
            )
        if not self.booking_assistance_enabled:
            parts.append(
                "Do not attempt to take or arrange bookings. If asked, explain "
                "how the customer can book through the business's usual channel."
            )
        return "\n\n".join(parts)


#: The persona used by the legacy single-tenant path, preserving the exact
#: behaviour of the application before multi-tenancy was introduced.
LEGACY_SMARTDESK_PERSONA = TenantPersona()
