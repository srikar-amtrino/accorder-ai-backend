"""Test fixtures.

The DI container builds a Bedrock client and loads a local embedding model at
import time, so the tests replace it with a stub module before the service
under test is imported. Tests plug in a fake model / clause search through
the ``container_stub`` fixture.
"""

import sys
import types
from typing import Any, Optional

import pytest


class _ContainerState:
    model: Any = None
    search: Any = None


_fake_container = types.ModuleType("src.core.container")
_fake_container.state = _ContainerState  # type: ignore[attr-defined]
_fake_container.get_bedrock_model = lambda: _ContainerState.model  # type: ignore[attr-defined]
_fake_container.get_clause_search = lambda: _ContainerState.search  # type: ignore[attr-defined]
sys.modules.setdefault("src.core.container", _fake_container)


@pytest.fixture
def container_stub():
    """Set the fake container's model / clause search for one test and reset afterwards."""

    def configure(model: Any = None, search: Optional[Any] = None) -> None:
        _ContainerState.model = model
        _ContainerState.search = search

    yield configure
    _ContainerState.model = None
    _ContainerState.search = None
