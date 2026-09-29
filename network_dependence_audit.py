"""Does the test suite depend on the network? Run it and read the list.

NOT IN CI, DELIBERATELY -- it is an audit, not a gate, and the correct answer
is not always "none". Like the other root-level audit tools it is opt-in:

    python -m pytest tests/ tests_core.py -q -p network_dependence_audit

WHY THIS EXISTS
---------------
Four to six `_call_openai` tests used to fail intermittently (~2 full-suite
runs in 7) with ``KeyError('body')`` -- a name that points at a local dict
rather than at anything that went wrong. The cause was not flakiness in the
code: every ``_call_openai`` / ``_call_gemini`` entry point validates the
provider endpoint BEFORE the request, ``ssrf.validate_endpoint`` calls
``is_private_host``, and that does a real DNS lookup. The tests faked the HTTP
call and not the validation, so a resolver hiccup turned into an early return
and then a KeyError. Fixed in 30e8428 by making the request-shape tests skip
the resolver.

Finding that one file at a time is slow, and "I did not find more" is a much
weaker statement than a measurement. This makes the whole question one run.

THE ANSWER TODAY
----------------
Five tests, and every one of them is, by name, a claim that a PUBLIC HOSTNAME
IS ACCEPTED:

    tests/test_ssrf.py::test_public_https_accepted
    tests/test_ssrf.py::test_public_hostname_accepted
    tests/test_ssrf_shared.py::test_public_https_accepted
    tests/test_gui_endpoint_validation.py::test_validate_endpoint_accepts_https_public
    tests/test_gui_fluent_ssrf.py::test_validate_endpoint_accepts_https_public

That is the right place for the dependency. "A hostname that resolves to a
public address is accepted" is a claim about name resolution; it cannot be made
offline, and rca_core/ssrf.py deliberately fails closed when resolution fails
("don't let an attacker bypass the check by passing an unresolvable name"). So
these five are expected here, and nothing else is.

CALIBRATION, which cost one wrong run
--------------------------------------
The stub must raise ``socket.gaierror``, NOT a bare ``OSError``.
``gaierror`` is a SUBCLASS of ``OSError``, and ``is_private_host`` catches
exactly ``socket.gaierror`` around ``getaddrinfo``. A bare OSError therefore
escapes that handler and propagates as an exception instead of being treated as
"unresolvable -> private -> refuse", which is not what a resolver outage looks
like. The first version of this sweep raised OSError and reported eight
failers, three of which (``test_ipv4_cloud_metadata_rejected`` and the two
``test_validate_endpoint_is_called_in_*``) never touch the network at all --
``is_private_host`` parses a literal IP and returns before resolving.

For the same reason the stub spares localhost and loopback: a real outage does
not break them, they come from the hosts file. Stubbing them would manufacture
failures and teach the wrong lesson.
"""
from __future__ import annotations

import ipaddress
import socket
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The tests that are SUPPOSED to be listed. If this list and the measured list
# disagree, one of them changed and the diff is the interesting part.
EXPECTED = {
    "tests/test_ssrf.py::TestSSRF::test_public_https_accepted",
    "tests/test_ssrf.py::TestSSRF::test_public_hostname_accepted",
    "tests/test_ssrf_shared.py::TestSharedSSRF::test_public_https_accepted",
    "tests/test_gui_endpoint_validation.py::TestGuiEndpointValidation"
    "::test_validate_endpoint_accepts_https_public",
    "tests/test_gui_fluent_ssrf.py::TestFluentSSRF::test_validate_endpoint_accepts_https_public",
}

_real_getaddrinfo = socket.getaddrinfo
_failures: list[str] = []


def _is_local(name: str) -> bool:
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _selective(host, *args, **kwargs):
    name = str(host).strip("[]").lower()
    if _is_local(name):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise socket.gaierror(
        f"network_dependence_audit: external resolution disabled ({name})")


def pytest_configure(config):
    socket.getaddrinfo = _selective


def pytest_unconfigure(config):
    socket.getaddrinfo = _real_getaddrinfo


def pytest_runtest_logreport(report):
    if report.when == "call" and report.failed:
        _failures.append(report.nodeid)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tw = terminalreporter.write_line
    measured = set(_failures)
    tw("")
    tw("=" * 72)
    tw("network_dependence_audit: external name resolution was disabled.")
    tw(f"{len(measured)} test(s) needed it:")
    for nodeid in sorted(measured):
        mark = "  (expected)" if nodeid in EXPECTED else "  <== UNEXPECTED"
        tw(f"  {nodeid}{mark}")
    unexpected = measured - EXPECTED
    missing = EXPECTED - measured
    for nodeid in sorted(unexpected):
        tw(f"  NEW network dependency: {nodeid}")
    for nodeid in sorted(missing):
        tw(f"  no longer network-dependent: {nodeid}")
    tw("=" * 72)
