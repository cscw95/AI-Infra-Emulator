"""Shared pytest fixtures for the NICo Emulator test suite."""
import os
# Cap the cluster for fast tests (covers su-1..su-2 referenced by the suite).
# The real server (run.sh, no env) seeds the full 140-rack cluster.
os.environ.setdefault("NICO_RACKS_LIMIT", "24")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE

STORE.reset()  # apply the capped cluster (module import already seeded full)


@pytest.fixture
def client():
    """Fresh TestClient with the store reset to the seeded twin before/after."""
    STORE.reset()
    with TestClient(app) as c:
        yield c
    STORE.reset()
