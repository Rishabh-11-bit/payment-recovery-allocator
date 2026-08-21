from __future__ import annotations

import pathlib

import pytest

from recovery.config import load_config
from recovery.gateway import SimulatedGateway
from recovery.store import Store


@pytest.fixture
def config():
    return load_config(pathlib.Path("config/default.yaml"))


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.initialise()
    yield store
    store.close()


@pytest.fixture
def gateway() -> SimulatedGateway:
    return SimulatedGateway()
