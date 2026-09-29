"""Enumerate the tests that depend on the machine's resolver.

NOT IN CI, DELIBERATELY -- like the other audit tools at the repo root. It is a
pytest plugin you opt into, and it makes the suite RED on purpose, so wiring it
into a build would be a self-inflicted outage. Run it when you add tests that
touch a provider endpoint, or after any change to the SSRF path.

WHAT IT DOES
------------
Kills external name resolution for the whole run -- ``socket.getaddrinfo``
raises for every name that is not ``localhost``, a ``*.localhost`` name, or a
literal IP -- then reports which tests failed. The calibration matters: a real
resolver outage does NOT break localhost (it comes from the hosts file) or
literal addresses, so a blanket kill produces failures for the wrong reason and
teaches you the wrong thing.

WHY
---
Every ``_call_openai`` / ``_call_gemini`` entry point, and
``server.py``'s ``_handle_extract_body``, validate the provider endpoint BEFORE
doing any I/O, and that validation calls ``ssrf.is_private_host``, which
resolves the host. A test that fakes the HTTP layer but not the validation is
therefore a test that quietly needs the internet. This happened three times in
one session (tests/test_llm_fixes.py, tests/test_sprint_b_pipeline.py,
tests/test_sprint_b_server.py), each time surfacing as an intermittent
KeyError on a local dict with no explanation.

USAGE
-----
    python -m pytest tests/ -q -p network_dependence_audit

READING THE OUTPUT
------------------
Anything that fails is a test whose result depends on the network. Sort them:

  * EXPECTED -- tests/test_ssrf.py, tests/test_ssrf_shared.py,
    tests/test_gui_endpoint_validation.py, tests/test_gui_fluent_ssrf.py.
    These assert that a PUBLIC https endpoint is ACCEPTED, which is a claim
    about name resolution and cannot be made without it. They are the reason
    the audit has an answer at all, and they should go red when it runs.
  * A BUG -- everything else. A test whose subject is request shape, export,
    merge, quality, the editor, or a server response does not need a resolver.
    Give it the same autouse fixture the three fixed files use: answer bare
    hostnames as public without a lookup, and leave literal IPs and localhost
    on the real predicate so the policy suites keep testing real behaviour.
"""
from __future__ import annotations

import ipaddress
import socket

__all__ = ["EXPECTED_NETWORK_DEPENDENT"]

_real_getaddrinfo = socket.getaddrinfo

# Suites whose subject IS name resolution. Listed explicitly so that a NEW
# unexpected failure stands out, instead of hiding among the known ones.
EXPECTED_NETWORK_DEPENDENT = (
    "tests/test_ssrf.py",
    "tests/test_ssrf_shared.py",
    "tests/test_gui_endpoint_validation.py",
    "tests/test_gui_fluent_ssrf.py",
)


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
    raise OSError("network_dependence_audit: external name resolution disabled")


def pytest_configure(config):
    socket.getaddrinfo = _selective


def pytest_unconfigure(config):
    socket.getaddrinfo = _real_getaddrinfo


def pytest_runtest_logreport(report):
    if report.when == "call" and report.failed:
        item = report.nodeid
        path = item.split("::", 1)[0].replace("\\", "/")
        kind = "EXPECTED" if path in EXPECTED_NETWORK_DEPENDENT else "A BUG"
        print(f"\n[netdep] {kind}: {item}")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tw = terminalreporter.write_line
    tw("")
    tw("=" * 72)
    tw("network_dependence_audit: ran with external name resolution disabled.")
    tw("Failures in the EXPECTED suites are correct -- they test DNS policy.")
    tw("Any A BUG failure is a unit test that does not need a resolver but")
    tw("got one anyway. See the module docstring for the fix.")
    tw("=" * 72)
