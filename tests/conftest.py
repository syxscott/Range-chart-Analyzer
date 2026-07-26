"""Pytest configuration including gold-standard test fixtures."""
from __future__ import annotations

import os
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "gold_smoke: gold-standard smoke tests (run on every PR)",
    )
    config.addinivalue_line(
        "markers",
        "gold: full gold-standard tests (run weekly / release)",
    )
    config.addinivalue_line(
        "markers",
        "gold_offline: gold tests using cached responses (zero API cost)",
    )


def pytest_collection_modifyitems(config, items):
    """Apply markers based on test file location and marker names."""
    for item in items:
        if "gold_smoke" in str(item.fspath):
            item.add_marker(pytest.mark.gold_smoke)
        elif "gold_metrics" in str(item.fspath):
            item.add_marker(pytest.mark.gold)
        elif "gold" in str(item.fspath):
            item.add_marker(pytest.mark.gold)


@pytest.fixture
def rca_offline_gold(request):
    """Force gold tests to use cached responses (no API calls).

    Set RCA_OFFLINE_GOLD=1 in the environment to activate.
    """
    return os.environ.get("RCA_OFFLINE_GOLD", "") == "1"
