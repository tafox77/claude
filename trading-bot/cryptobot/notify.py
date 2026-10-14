"""Optional webhook alerts (Discord, Slack or any endpoint accepting JSON)."""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    def send(self, text: str) -> None:
        if not self.webhook_url:
            return
        # "content" is read by Discord, "text" by Slack.
        body = json.dumps({"content": text, "text": text}).encode()
        req = urllib.request.Request(
            self.webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # alerts must never take the bot down
            log.warning("webhook notification failed: %s", e)
