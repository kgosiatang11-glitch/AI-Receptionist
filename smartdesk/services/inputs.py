"""Strict parsing of user-controlled input (C6).

``bool(value)`` is wrong for request data: ``bool("false")`` is ``True``.  A
string that *looks* like a refusal would silently grant (or fail to revoke) an
administrative flag.  Booleans from a request body are therefore parsed here
and anything that is not an unambiguous boolean is rejected, never coerced.
"""

from __future__ import annotations


class InvalidInput(ValueError):
    """A request value is not acceptable.  The message is safe to show."""

    def __init__(self, field: str, message: str | None = None):
        super().__init__(message or f"{field} must be true or false")
        self.field = field


_TRUE_STRINGS = frozenset({"true"})
_FALSE_STRINGS = frozenset({"false"})


def parse_strict_bool(value, field: str) -> bool:
    """Return ``value`` as a bool, or raise :class:`InvalidInput`.

    Accepted: JSON ``true`` / ``false`` and the strings ``"true"`` / ``"false"``
    (any case, surrounding whitespace ignored).  Rejected: ``None``, numbers
    (including ``0`` / ``1``), other strings, lists and objects.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    raise InvalidInput(field)
