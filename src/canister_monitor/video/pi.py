"""Talking to the Pi's dashboard API from the laptop app, with the standard library only."""

import json
import urllib.error
import urllib.request


class PiError(Exception):
    pass


class PiClient:
    def __init__(self, base_url: str, timeout: float = 4.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def bounce(self) -> dict:
        """Setups, batches, settings: ``GET /api/bounce``."""
        return self._get("/api/bounce")

    def _get(self, path: str) -> dict:
        url = self.base_url + path
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise PiError(f"{url}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise PiError(f"can't reach {self.base_url}: {reason}") from None
        except ValueError:
            raise PiError(f"{url}: not a JSON reply") from None
