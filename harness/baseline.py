"""Pre-existing test failures that are NOT model failures (SPIKE section (b)).

A task is scored FAIL only when a failing test name falls outside its project's allowlist.
These four libp2p names were established on an unmodified tree: three are environment
(no IPv6 egress / no IPv6 multicast on this host, live-DNS test) and one is a confirmed
timing flake. charset-normalizer and iceberg have empty baselines.
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
            "connection::tests::idle_timeout_with_keep_alive_no",  # libp2p-swarm, timing flake
        }
    ),
}

# Known-flaky test names. If one of these is the only thing standing between a task and a
# PASS, re-run it by name before letting it count.
FLAKY_TESTS: frozenset[str] = frozenset(
    {"connection::tests::idle_timeout_with_keep_alive_no"}
)

FLAKY_RETRIES = 3


def allowlist(project: str) -> frozenset[str]:
    return BASELINE_FAILURES.get(project, frozenset())
