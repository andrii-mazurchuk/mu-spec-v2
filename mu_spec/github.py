"""The unit's only outbound client: one HTTP request to GitHub per call, and its answer.

GitHubClient sends exactly one request per request() call and returns whatever status came back,
4xx and 5xx included, with the body decoded and the header names lower-cased. It never retries,
never sleeps and never paces: that policy is the Ship part's sender's, which reads the answer's
headers and, when no answer came, GitHubUnreachable.timed_out. build_runtime builds the one
instance from Config.github_token and hands it to the Ship part alone.

Stdlib urllib.request, as the running unit uses it, so the same TLS trust and the same failure
texts come out of the same calls.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import types
import urllib.error
import urllib.request
from collections.abc import Mapping

API_ROOT: str = "https://api.github.com"
TIMEOUT_SECONDS: float = 30.0
REQUEST_HEADERS: tuple[tuple[str, str], ...] = (
    ("Accept", "application/vnd.github+json"),
    ("X-GitHub-Api-Version", "2026-03-10"),
    ("User-Agent", "mu-spec"),
)

METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})


class GitHubUnreachable(Exception):
    """No answer came: timed_out when the request was sent and TIMEOUT_SECONDS passed first."""

    def __init__(self, reason: str, timed_out: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.timed_out = timed_out

    def __repr__(self) -> str:
        return f"GitHubUnreachable({self.reason!r}, timed_out={self.timed_out!r})"


@dataclasses.dataclass(frozen=True)
class GitHubResponse:
    status: int
    body: object
    headers: Mapping[str, str]


def _decode(raw: bytes) -> object:
    """Decoded JSON, None for an empty body, the text itself when it is not JSON."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _headers(message: object) -> Mapping[str, str]:
    items = message.items() if message is not None else ()  # type: ignore[attr-defined]
    return types.MappingProxyType({name.lower(): value for name, value in items})


class GitHubClient:
    """A token, or none, and one request at a time."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "GitHubClient(...)"

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> GitHubResponse:
        method = method.upper()
        if method not in METHODS:
            raise ValueError(f"not a GitHub request method: {method!r}")
        headers = dict(REQUEST_HEADERS)
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            API_ROOT.rstrip("/") + "/" + path.lstrip("/"), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return GitHubResponse(response.status, _decode(response.read()), _headers(response.headers))
        except urllib.error.HTTPError as exc:
            # an answer with a status, returned as any other
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            return GitHubResponse(exc.code, _decode(raw), _headers(exc.headers))
        except urllib.error.URLError as exc:
            # nothing was sent, or nothing could be: a connect that timed out is among these
            raise GitHubUnreachable(str(exc.reason), False) from None
        except TimeoutError as exc:
            # the request was sent and its answer did not come within TIMEOUT_SECONDS
            raise GitHubUnreachable(str(exc), True) from None
        except (OSError, http.client.HTTPException) as exc:
            raise GitHubUnreachable(str(exc), False) from None
