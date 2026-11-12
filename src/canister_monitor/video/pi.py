"""Talking to the Pi's dashboard API from the laptop app, with the standard library only."""

import json
import urllib.error
import urllib.request

SAVE_TIMEOUT_S = 30.0  # a long session's pictures are a few MB over Wi-Fi


class PiError(Exception):
    """``status`` is the Pi's HTTP status if it answered, else None (not reachable)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retry_later(self) -> bool:
        """Worth keeping the request to send again: the Pi didn't answer, or failed itself
        (a 4xx means the request is wrong and would fail again)."""
        return self.status is None or self.status >= 500


class PiClient:
    def __init__(self, base_url: str, timeout: float = 4.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def bounce(self) -> dict:
        """Setups, batches, settings: ``GET /api/bounce``."""
        return self._request("GET", "/api/bounce")

    def canisters(self) -> dict:
        """Latest reading of each sensor: ``GET /api/canisters``."""
        return self._request("GET", "/api/canisters")

    def save_video_test(self, doc: dict) -> tuple[int, dict]:
        """``POST /api/bounce/video-tests``: (201 new or 200 replaced, the saved test)."""
        return self._request("POST", "/api/bounce/video-tests", doc, SAVE_TIMEOUT_S, status=True)

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float | None = None,
        status: bool = False,
    ):
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                reply = json.loads(resp.read())
                return (resp.status, reply) if status else reply
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("error")
            except (ValueError, AttributeError, OSError):
                why = None
            raise PiError(
                f"the Pi refused: {why}" if why else f"{url}: HTTP {e.code}", e.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise PiError(f"can't reach {self.base_url}: {reason}") from None
        except ValueError:
            raise PiError(f"{url}: not a JSON reply", 502) from None
