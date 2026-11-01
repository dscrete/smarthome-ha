"""Load the integration's pure media modules without importing Home Assistant.

The package ``__init__`` imports HA, so these tests register a bare package
pointing at the integration folder and import only modules with no HA imports.
"""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import types

import pytest

_PACKAGE = "smarthome_media_under_test"
_ROOT = Path(__file__).resolve().parents[1]
_FOLDER = _ROOT / "custom_components" / "smarthome_assistant"
# Home Assistant-level tests import the integration as ``custom_components``.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    # Import this repository's package before the HA test harness can resolve
    # ``custom_components`` to its own bundled testing package.
    import custom_components.smarthome_assistant  # noqa: F401
except ImportError:  # Home Assistant is not installed: only pure tests run.
    pass

if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_FOLDER)]
    sys.modules[_PACKAGE] = package


@pytest.fixture(scope="session")
def media_match():
    return importlib.import_module(f"{_PACKAGE}.media_match")


@pytest.fixture(scope="session")
def media_targets():
    return importlib.import_module(f"{_PACKAGE}.media_targets")
