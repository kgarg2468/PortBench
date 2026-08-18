"""Pre-existing test failures that are NOT model failures (SPIKE section (b)).

A task is scored FAIL only when a failing test name falls outside its project's allowlist.
The allowlist holds *environment* failures only: tests that can never pass on this host no
matter what the model writes (no IPv6 egress, no IPv6 multicast, a live-DNS lookup). Those are
subtracted unconditionally, because a model has no way to affect them.

A **flake** is a different thing and is deliberately NOT allowlisted. Allowlisting it would
subtract it unconditionally -- including when the model's own code is what made it fail -- and
would also make the flaky-retry path unreachable, since the name never reaches the retry loop.
Flakes go through `FLAKY_TESTS` instead: the test is re-run, and only a rerun that actually
executes it and comes back clean lets it count.

charset-normalizer and iceberg have empty baselines.
"""

from __future__ import annotations

BASELINE_FAILURES: dict[str, frozenset[str]] = {
    "charset-normalizer": frozenset(),
    "iceberg": frozenset(),
    "libp2p": frozenset(
        {
            "tests::basic_resolve",                              # libp2p-dns, live DNS + IPv6
            "test_discovery_async_std_ipv6",                     # libp2p-mdns, IPv6 multicast
            "test_discovery_tokio_ipv6",                         # libp2p-mdns, IPv6 multicast
        }
    ),
}

# Known-flaky test names. If one of these is the only thing standing between a task and a
# PASS, re-run it by name before letting it count. Not allowlisted: see the module docstring.
FLAKY_TESTS: frozenset[str] = frozenset(
    {"connection::tests::idle_timeout_with_keep_alive_no"}  # libp2p-swarm, timing flake
)

FLAKY_RETRIES = 3


def allowlist(project: str) -> frozenset[str]:
    return BASELINE_FAILURES.get(project, frozenset())
