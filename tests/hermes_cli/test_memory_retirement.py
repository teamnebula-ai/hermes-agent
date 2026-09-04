from hermes_cli.memory_setup import _get_available_providers


def test_retired_provider_is_not_discoverable():
    providers = {name for name, _setup, _provider in _get_available_providers()}
    assert "mem0" not in providers
