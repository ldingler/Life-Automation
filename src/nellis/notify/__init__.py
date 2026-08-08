"""Email notifications and alert deduplication."""

from . import dedupe
from .email import EmailNotifier, RenderedEmail, send_closing_soon, send_digest

__all__ = ["EmailNotifier", "RenderedEmail", "dedupe", "send_closing_soon", "send_digest"]
