"""Shared rules for read-only OAuth requests."""

from email.utils import parsedate_to_datetime
from urllib.request import HTTPRedirectHandler

from .common import number

MAX_RESPONSE = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an OAuth bearer token to a redirect target.
        return None


def retry_time(value, now):
    minimum = now + 60
    try:
        seconds = float(value)
        if number(seconds):
            return max(minimum, now + seconds)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        stamp = parsedate_to_datetime(value).timestamp()
        return max(minimum, stamp) if number(stamp) else minimum
    except (TypeError, ValueError, OverflowError, IndexError):
        return minimum
