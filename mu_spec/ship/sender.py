"""The Ship part's one caller of GitHubClient.request: one pause and retry rule for every request.

Every write send is preceded by WRITE_PAUSE_S, a repeated one included; reads never pause. A
request is tried up to MAX_ATTEMPTS times on a retryable status or on a failed connection, waiting
Retry-After when the answer carries it, else BACKOFF_S. A write that timed out after connecting is
raised at once and never sent again, because it may already have landed. Waiting only delays: no
request and no order depends on time. The reason recorded for a raised GitHubUnreachable is the
plan's to choose; this module adds no text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from mu_spec.github import GitHubClient, GitHubResponse, GitHubUnreachable

WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
WRITE_PAUSE_S = 1.0        # slept before every send of a write, a repeated one included; reads never pause
MAX_ATTEMPTS = 4           # tries in total for one request, first included
BACKOFF_S = (1.0, 2.0, 4.0)   # wait before the 2nd, 3rd and 4th try when no Retry-After applies
RETRY_STATUSES = (429, 500, 501, 502, 503, 504)   # plus any other 5xx, and a 403 whose headers['x-ratelimit-remaining'] == '0'
SHIP_GITHUB_HOLDERS = ("mu_spec.ship.part", "mu_spec.ship.sender", "mu_spec.ship.plan", "mu_spec.ship.access",
                       "mu_spec.actions")   # this slice's GITHUB_HOLDERS members; 5
CLIENT_NAMERS = ("mu_spec.ship.part", "mu_spec.ship.sender")   # the SHIP_GITHUB_HOLDERS members that name GitHubClient; 2


@dataclass(frozen=True)
class Answer:
    status: int                      # GitHubResponse.status
    body: object                     # GitHubResponse.body: decoded JSON, None for an empty body
    headers: Mapping[str, str]       # GitHubResponse.headers, names lower-cased (iss-1961)


def _answer(response: GitHubResponse) -> Answer:
    return Answer(status=response.status, body=response.body, headers=response.headers)


def _retryable(answer: Answer) -> bool:
    if answer.status in RETRY_STATUSES or 500 <= answer.status <= 599:
        return True
    return answer.status == 403 and answer.headers.get("x-ratelimit-remaining") == "0"


class Sender:
    """Sends one request through the client under the pause and retry rule."""

    def __init__(self, client: GitHubClient, sleep: Callable[[float], None], monotonic: Callable[[], float]) -> None:
        self._client = client
        self._sleep = sleep
        self._monotonic = monotonic

    def send(self, method: str, path: str, body: Mapping[str, object] | None = None) -> Answer:
        write = method.upper() in WRITE_METHODS
        for k in range(1, MAX_ATTEMPTS + 1):
            if write:
                self._sleep(WRITE_PAUSE_S)
            try:
                answer = _answer(self._client.request(method, path, body))
            except GitHubUnreachable as exc:
                if (exc.timed_out and write) or k == MAX_ATTEMPTS:
                    raise
                self._sleep(BACKOFF_S[k - 1])
                continue
            if not _retryable(answer) or k == MAX_ATTEMPTS:
                return answer
            retry_after = answer.headers.get("retry-after")
            self._sleep(float(retry_after) if retry_after is not None else BACKOFF_S[k - 1])
        raise AssertionError("unreachable: the last try returns or raises")
