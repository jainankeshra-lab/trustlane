"""Test defaults: no store offers unless a test opts in, so prices in older tests stay at shelf price."""
import pytest


@pytest.fixture(autouse=True)
def _no_offers(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("TRUSTLANE_OFFERS", str(tmp_path_factory.getbasetemp() / "no-offers.json"))
