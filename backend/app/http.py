"""One place that creates outbound HTTP clients (Canvas, Google, breached-password check).

Tests swap in a fake transport here, so no test ever reaches the real internet.
"""

import httpx2 as httpx

# Set by tests to an httpx.MockTransport. None means real network.
transport: httpx.BaseTransport | None = None

USER_AGENT = "Defog/1.0 (+https://defog.fit)"


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    )
