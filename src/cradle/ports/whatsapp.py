"""WhatsApp echo port: post every logged event to a WhatsApp chat (task N5).

Deliberately a separate port from ports/notifier.py's Notifier Protocol: that
one is typed to Finding (alert findings from the rules engine, keyed by
severity); this fires on raw data entry regardless of whether any rule ever
evaluates it, and carries a plain preformatted line, not a Finding. Overloading
Notifier.send(finding) for a plain string would be the wrong signature to
reuse (task notes).

Transport: Meta's WhatsApp Cloud API - a bearer-token HTTPS POST, chosen for
the same reason N1/D3 chose ntfy: no new dependency (SPEC 6, closed set),
implemented over httpx exactly like NtfyNotifier.
"""

import logging
from typing import Protocol

log = logging.getLogger(__name__)

API_VERSION = "v20.0"
TIMEOUT_SECONDS = 10.0


class Poster(Protocol):
    """The HTTP seam of the WhatsApp adapter: httpx.post live, a double in tests.

    Typed as a Protocol rather than `object` so the injected substitute has to
    accept the call the adapter actually makes.
    """

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: float,
    ) -> object: ...


class WhatsAppNotifier(Protocol):
    @property
    def configured(self) -> bool: ...

    def send(self, text: str) -> bool: ...


class WhatsAppCloudNotifier:
    """POST a preformatted line to a WhatsApp chat via Meta's Cloud API (task N5).

    Never raises: a delivery failure must not abort the caller or lose the
    underlying event, which is already persisted by the time this is called -
    the same "never raises" contract NtfyNotifier's docstring states for ntfy
    (N1). Unlike NtfyNotifier, send() reports back whether delivery looked
    clean (2xx) so the caller can record the outcome in chat_log_repo for its
    audit trail, without ever needing to raise to do so.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        chat_id: str,
        poster: Poster | None = None,
    ) -> None:
        self._access_token = access_token.strip()
        self._phone_number_id = phone_number_id.strip()
        self._chat_id = chat_id.strip()
        self._poster = poster

    @property
    def configured(self) -> bool:
        return bool(self._access_token and self._phone_number_id and self._chat_id)

    @property
    def url(self) -> str:
        return f"https://graph.facebook.com/{API_VERSION}/{self._phone_number_id}/messages"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def payload(self, text: str) -> dict[str, object]:
        return {
            "messaging_product": "whatsapp",
            "to": self._chat_id,
            "type": "text",
            "text": {"body": text},
        }

    def send(self, text: str) -> bool:
        """POST *text* as one WhatsApp message. Returns whether it went out clean."""
        if not self.configured:
            log.warning("WhatsApp destination not configured; dropping message")
            return False
        try:
            post = self._poster
            if post is None:
                import httpx  # noqa: PLC0415 - keep import cost off the hot path

                post = httpx.post
            response = post(
                self.url,
                json=self.payload(text),
                headers=self.headers(),
                timeout=TIMEOUT_SECONDS,
            )
            status = getattr(response, "status_code", None)
            if status is not None and not (200 <= status < 300):
                log.warning("WhatsApp delivery returned status %s", status)
                return False
            return True
        except Exception:
            log.exception("WhatsApp delivery failed")
            return False
