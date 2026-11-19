"""Contract: package metadata, public exports and the exception hierarchy."""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import subprocess
import sys

import pytest
from fastmcp.exceptions import ToolError

import ai_economy_sdk
from ai_economy_sdk import exceptions as exc


def test_version_matches_distribution_metadata():
    assert ai_economy_sdk.__version__ == importlib.metadata.version("ai-economy-sdk")


def test_py_typed_marker_is_shipped():
    assert importlib.resources.files("ai_economy_sdk").joinpath("py.typed").is_file()


def test_all_exports_exist():
    for name in ai_economy_sdk.__all__:
        assert hasattr(ai_economy_sdk, name), name


@pytest.mark.parametrize(
    ("cls", "builtin"),
    [
        (exc.ConfigurationError, ValueError),
        (exc.UnknownNetworkError, ValueError),
        (exc.AlgodClientRequiredError, RuntimeError),
        (exc.AlgodClientRequiredError, ValueError),
        (exc.PaymentVerificationError, RuntimeError),
        (exc.PaymentRequiredError, ToolError),
        (exc.SettlementError, ToolError),
    ],
)
def test_exceptions_keep_their_previous_builtin_base(cls, builtin):
    assert issubclass(cls, exc.AIEconomyError)
    assert issubclass(cls, builtin)
    assert getattr(ai_economy_sdk, cls.__name__) is cls


def test_unknown_network_raises_typed_error():
    with pytest.raises(exc.UnknownNetworkError):
        ai_economy_sdk.resolve_network("not-a-network")
    with pytest.raises(ValueError):
        ai_economy_sdk.resolve_network("")


def test_import_emits_no_deprecation_warnings():
    out = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", "import ai_economy_sdk"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_library_logger_has_null_handler():
    import logging

    handlers = logging.getLogger("ai_economy_sdk").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)
