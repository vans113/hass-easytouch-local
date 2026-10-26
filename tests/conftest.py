"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load custom_components/easytouch_local."""
    yield


@pytest.fixture(autouse=True)
def allow_localhost(socket_enabled):
    """The fake controller is a real TCP server on 127.0.0.1."""
    yield
