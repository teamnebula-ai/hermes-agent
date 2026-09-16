"""Tests for same-credential model hops in the quota failover chain.

A ``fallback_providers`` entry on the same provider and base_url as the live
runtime is a second model on the SAME credential (gpt-5.5 -> gpt-5.3-codex-spark
on one ChatGPT account), not a provider failover.  Two helpers implement it:

* ``run_agent._next_fallback_is_same_credential`` decides whether the next
  chain entry is such a hop, so it can be taken before pool rotation.
* ``agent.agent_runtime_helpers.restore_primary_model_for_rotation`` puts the
  primary model back before the pool rotates to another credential, while
  keeping the chain index so the spent hop is not taken again.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import agent_runtime_helpers
from agent.agent_runtime_helpers import restore_primary_model_for_rotation
from run_agent import AIAgent, FailoverReason, _next_fallback_is_same_credential


PRIMARY_URL = "https://my-llm.example.com/v1"


def _hop_agent(**overrides):
    """A plain object carrying only the fields the hop detector reads."""
    fields = {
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "_fallback_index": 0,
        "_fallback_chain": [
            {
                "provider": "openai-codex",
                "model": "gpt-5.3-codex-spark",
                "base_url": "https://chatgpt.com/backend-api/codex",
            },
            {"provider": "anthropic", "model": "claude-opus-4"},
        ],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(fallback_model):
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url=PRIMARY_URL,
            provider="custom",
            model="primary-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = MagicMock()
    return agent


def _fallback_client(base_url=PRIMARY_URL, api_key="fallback-key-1234"):
    client = MagicMock()
    client.api_key = api_key
    client.base_url = base_url
    client._custom_headers = None
    client.default_headers = None
    return client


def _activate_next_fallback(agent, base_url=PRIMARY_URL, reason=None):
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(_fallback_client(base_url=base_url), None),
    ):
        return agent._try_activate_fallback(reason=reason)


# =============================================================================
# _next_fallback_is_same_credential
# =============================================================================

class TestNextFallbackIsSameCredential:
    def test_same_account_model_hop_detected(self):
        assert _next_fallback_is_same_credential(_hop_agent()) is True

    def test_match_ignores_case_whitespace_and_trailing_slash(self):
        agent = _hop_agent(
            provider=" OpenAI-Codex ",
            base_url="https://ChatGPT.com/backend-api/codex/",
        )
        assert _next_fallback_is_same_credential(agent) is True

    def test_entry_without_base_url_counts_as_same_account(self):
        agent = _hop_agent(
            _fallback_chain=[{"provider": "openai-codex", "model": "gpt-5.3-codex-spark"}],
        )
        assert _next_fallback_is_same_credential(agent) is True

    def test_cross_provider_entry_ignored(self):
        agent = _hop_agent(
            _fallback_chain=[{"provider": "anthropic", "model": "claude-opus-4"}],
        )
        assert _next_fallback_is_same_credential(agent) is False

    def test_identical_model_rejected(self):
        agent = _hop_agent(
            _fallback_chain=[{
                "provider": "openai-codex",
                "model": "GPT-5.5",
                "base_url": "https://chatgpt.com/backend-api/codex",
            }],
        )
        assert _next_fallback_is_same_credential(agent) is False

    def test_differing_base_url_rejected(self):
        agent = _hop_agent(
            _fallback_chain=[{
                "provider": "openai-codex",
                "model": "gpt-5.3-codex-spark",
                "base_url": "https://other-proxy.example.com/codex",
            }],
        )
        assert _next_fallback_is_same_credential(agent) is False

    def test_spent_hop_is_not_offered_again(self):
        # Index 1 means the spark hop was already taken; the next entry is
        # the cross-provider failover.
        assert _next_fallback_is_same_credential(_hop_agent(_fallback_index=1)) is False

    def test_exhausted_chain_returns_false(self):
        assert _next_fallback_is_same_credential(_hop_agent(_fallback_index=2)) is False
        assert _next_fallback_is_same_credential(_hop_agent(_fallback_index=99)) is False

    def test_index_selects_later_same_credential_entry(self):
        agent = _hop_agent(
            _fallback_index=1,
            _fallback_chain=[
                {"provider": "anthropic", "model": "claude-opus-4"},
                {"provider": "openai-codex", "model": "gpt-5.3-codex-spark"},
            ],
        )
        assert _next_fallback_is_same_credential(agent) is True

    @pytest.mark.parametrize(
        "agent",
        [
            SimpleNamespace(),
            SimpleNamespace(provider="openai-codex", model="gpt-5.5"),
            _hop_agent(_fallback_chain=None),
            _hop_agent(_fallback_chain=[None]),
            _hop_agent(_fallback_chain=[{"provider": "openai-codex"}]),
            _hop_agent(_fallback_chain=[{"model": "gpt-5.3-codex-spark"}]),
            _hop_agent(_fallback_index=None),
            _hop_agent(_fallback_index="not-a-number"),
            _hop_agent(provider=None, model=None, base_url=None),
        ],
    )
    def test_missing_or_malformed_attributes_never_raise(self, agent):
        result = _next_fallback_is_same_credential(agent)
        assert isinstance(result, bool)

    def test_missing_attributes_return_false(self):
        assert _next_fallback_is_same_credential(SimpleNamespace()) is False
        assert _next_fallback_is_same_credential(_hop_agent(_fallback_chain=[None])) is False
        assert _next_fallback_is_same_credential(
            _hop_agent(_fallback_index="not-a-number")
        ) is False


# =============================================================================
# restore_primary_model_for_rotation (unit)
# =============================================================================

class TestRestorePrimaryModelForRotationUnit:
    def _agent(self, **overrides):
        fields = {
            "_fallback_activated": True,
            "_primary_runtime": {"provider": "openai-codex", "model": "gpt-5.5"},
            "provider": "openai-codex",
            "model": "gpt-5.3-codex-spark",
            "_fallback_index": 1,
            "_rate_limited_until": time.monotonic() + 60,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_noop_when_no_fallback_active(self):
        agent = self._agent(_fallback_activated=False)
        with patch.object(agent_runtime_helpers, "restore_primary_runtime") as restore:
            assert restore_primary_model_for_rotation(agent) is False
        restore.assert_not_called()
        assert agent.model == "gpt-5.3-codex-spark"

    def test_cross_provider_fallback_left_alone(self):
        agent = self._agent(provider="anthropic", model="claude-opus-4")
        with patch.object(agent_runtime_helpers, "restore_primary_runtime") as restore:
            assert restore_primary_model_for_rotation(agent) is False
        restore.assert_not_called()
        assert agent.provider == "anthropic"

    def test_noop_when_already_on_primary_model(self):
        agent = self._agent(model="gpt-5.5")
        with patch.object(agent_runtime_helpers, "restore_primary_runtime") as restore:
            assert restore_primary_model_for_rotation(agent) is False
        restore.assert_not_called()

    def test_restore_holds_chain_index_and_clears_cooldown(self):
        agent = self._agent(_fallback_index=1)
        seen = {}

        def fake_restore(a):
            # The cooldown belongs to the credential being left, so it must be
            # cleared before restore_primary_runtime checks it.
            seen["cooldown"] = a._rate_limited_until
            a.model = a._primary_runtime["model"]
            a._fallback_activated = False
            a._fallback_index = 0
            return True

        with patch.object(agent_runtime_helpers, "restore_primary_runtime", side_effect=fake_restore):
            assert restore_primary_model_for_rotation(agent) is True

        assert seen["cooldown"] == 0
        assert agent.model == "gpt-5.5"
        assert agent._fallback_activated is False
        assert agent._fallback_index == 1
        assert agent._rate_limited_until == 0

    def test_failed_restore_keeps_cooldown_and_index(self):
        cooldown = time.monotonic() + 60
        agent = self._agent(_fallback_index=1, _rate_limited_until=cooldown)

        def failing_restore(a):
            a._fallback_index = 0
            return False

        with patch.object(agent_runtime_helpers, "restore_primary_runtime", side_effect=failing_restore):
            assert restore_primary_model_for_rotation(agent) is False

        assert agent._fallback_index == 1
        assert agent._rate_limited_until == cooldown
        assert agent.model == "gpt-5.3-codex-spark"

    @pytest.mark.parametrize(
        "agent",
        [
            SimpleNamespace(),
            SimpleNamespace(_fallback_activated=True),
            SimpleNamespace(_fallback_activated=True, _primary_runtime=None, provider="x"),
            SimpleNamespace(_fallback_activated=True, _primary_runtime={}, provider=None),
        ],
    )
    def test_missing_attributes_are_safe(self, agent):
        with patch.object(agent_runtime_helpers, "restore_primary_runtime") as restore:
            assert restore_primary_model_for_rotation(agent) is False
        restore.assert_not_called()


# =============================================================================
# End-to-end on a real AIAgent: hop, rotate, then provider failover
# =============================================================================

class TestSameCredentialHopRestorePath:
    CHAIN = [
        {"provider": "custom", "model": "spark-model", "base_url": PRIMARY_URL},
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
    ]

    def _pool(self, next_entry):
        pool = MagicMock()
        pool.provider = "custom"
        pool.has_credentials.return_value = True
        pool.mark_exhausted_and_rotate.return_value = next_entry
        return pool

    def test_hop_detected_then_spent(self):
        agent = _make_agent(self.CHAIN)
        assert _next_fallback_is_same_credential(agent) is True

        assert _activate_next_fallback(agent) is True
        assert agent.model == "spark-model"
        assert agent.provider == "custom"
        assert agent._fallback_index == 1
        # The hop is spent; the next entry is a real provider failover.
        assert _next_fallback_is_same_credential(agent) is False

    def test_rotation_restores_primary_model_before_swapping_credential(self):
        agent = _make_agent(self.CHAIN)
        assert _activate_next_fallback(agent, reason=FailoverReason.rate_limit) is True
        assert agent.model == "spark-model"
        assert agent._rate_limited_until > time.monotonic()

        next_entry = MagicMock(id="cred-2")
        agent._credential_pool = self._pool(next_entry)
        seen = {}

        def swap(entry):
            seen["model_at_swap"] = agent.model
            seen["entry"] = entry

        agent._swap_credential = MagicMock(side_effect=swap)

        with patch("run_agent.OpenAI", return_value=MagicMock()):
            recovered, has_retried = agent._recover_with_credential_pool(
                status_code=402, has_retried_429=False,
            )

        assert (recovered, has_retried) == (True, False)
        assert seen == {"model_at_swap": "primary-model", "entry": next_entry}
        assert agent.model == "primary-model"
        assert agent.provider == "custom"
        assert agent._fallback_activated is False
        assert agent._fallback_index == 1
        assert agent._rate_limited_until == 0

    def test_after_rotation_chain_continues_past_spent_hop(self):
        agent = _make_agent(self.CHAIN)
        assert _activate_next_fallback(agent) is True

        agent._credential_pool = self._pool(MagicMock(id="cred-2"))
        agent._swap_credential = MagicMock()
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            agent._recover_with_credential_pool(status_code=402, has_retried_429=False)

        # Every credential spent: the next activation is the cross-provider
        # entry, not the model hop again.
        assert _activate_next_fallback(agent, base_url="https://openrouter.ai/api/v1") is True
        assert agent.provider == "openrouter"
        assert agent.model == "anthropic/claude-sonnet-4"
        assert agent._fallback_index == 2

    def test_rotation_on_cross_provider_fallback_does_not_restore(self):
        agent = _make_agent([{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}])
        assert _activate_next_fallback(agent, base_url="https://openrouter.ai/api/v1") is True
        assert agent.provider == "openrouter"

        pool = self._pool(MagicMock(id="or-2"))
        pool.provider = "openrouter"
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()

        with patch.object(agent_runtime_helpers, "restore_primary_runtime") as restore:
            recovered, _ = agent._recover_with_credential_pool(
                status_code=402, has_retried_429=False,
            )

        assert recovered is True
        restore.assert_not_called()
        assert agent.provider == "openrouter"
        assert agent._fallback_activated is True
