"""URL converters shared by the API blueprints."""

from __future__ import annotations

import uuid

from werkzeug.routing import BaseConverter, ValidationError


class UUIDStrConverter(BaseConverter):
    """A path segment that must be a UUID; yields the canonical lowercase string.

    A malformed id never matches the route, so the request gets the app's
    ordinary JSON 404 instead of reaching a query where, on PostgreSQL, a bad
    value for a ``uuid`` column is a database error (a 500) -- and so it is
    indistinguishable from "not found in your tenant".  (Flask's built-in
    ``uuid`` converter would hand the views a ``UUID`` object, which does not
    compare equal to the string ids the models use.)
    """

    regex = r"[0-9a-fA-F-]{32,36}"

    def to_python(self, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValidationError() from None

    def to_url(self, value) -> str:
        return str(value)
