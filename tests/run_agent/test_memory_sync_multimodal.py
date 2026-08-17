"""Regression guard — external memory sync must flatten multimodal content.

Multimodal turns arrive from the API server with ``content`` as a list of
typed parts (``{"type": "text", ...}`` / ``{"type": "image_url", ...}``)
rather than a plain string.  ``_sync_external_memory_for_turn`` forwarded
``original_user_message`` to ``memory_manager.sync_all`` untouched, so every
memory provider received that list verbatim.

Providers feed the value straight into text APIs.  Mem0 v3 rejected it at
``POST /v3/memories/add/``::

    {'messages': [{'content': [ErrorDetail(string='Not a valid string.',
                                           code='invalid')]}, {}]}

Index 0 (the user message) failed validation while index 1 (the assistant's
plain-string reply) passed — the signature of exactly one non-string content
field.  Observed on the ``screddy-engagemate`` profile, whose worker submits
Instagram story evidence with attached media, so every one of its turns was
silently dropped from long-term memory.

The repo already ships ``_summarize_user_message_for_log``, which flattens a
part list to text and records attachments as an ``[N image(s)]`` marker so a
turn isn't stored as though the image never existed.  ``run_agent`` imported
it but only re-exported it for tests; the sync path never called it.  The fix
routes both sync arguments and the prefetch query through that helper.

Tests exercise the helper directly on a bare ``AIAgent`` built via
``__new__``, matching ``tests/run_agent/test_memory_sync_interrupted.py``.
"""
from unittest.mock import MagicMock

import pytest


TEXT_PART = {"type": "text", "text": "Evaluate this story"}
IMAGE_PART = {"type": "image_url", "image_url": {"url": "https://cdn/x.jpg"}}


def _bare_agent():
    """An ``AIAgent`` carrying only what ``_sync_external_memory_for_turn``
    touches — the same bare-agent pattern used by the interrupted-turn
    regression tests."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = MagicMock()
    agent.session_id = "test_session_001"
    return agent


def _sync_args(agent):
    """Positional ``(user, assistant)`` handed to ``sync_all``."""
    agent._memory_manager.sync_all.assert_called_once()
    args, _ = agent._memory_manager.sync_all.call_args
    return args


class TestMultimodalMemorySync:
    # --- The bug: a list must never reach a provider --------------------

    def test_multimodal_user_message_is_flattened_to_a_string(self):
        """The direct reproduction of the Mem0 400.  A part list must be
        flattened before it reaches ``sync_all``; anything else hands the
        provider a value its text API rejects."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=[TEXT_PART, IMAGE_PART],
            final_response="Reply: approve.",
            interrupted=False,
        )
        user_arg, assistant_arg = _sync_args(agent)
        assert isinstance(user_arg, str), (
            f"sync_all received {type(user_arg).__name__}, not str — Mem0 v3 "
            "rejects a list content field with 'Not a valid string.'"
        )
        assert isinstance(assistant_arg, str)

    def test_attached_images_are_recorded_not_dropped(self):
        """Flattening must not silently discard the attachment — the turn
        should record that media was present, or recall later implies the
        user sent text alone."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=[TEXT_PART, IMAGE_PART],
            final_response="Reply: approve.",
            interrupted=False,
        )
        user_arg, _ = _sync_args(agent)
        assert user_arg == "[1 image] Evaluate this story"

    def test_image_only_message_still_syncs(self):
        """An evidence-only turn carries no text part.  It must still sync
        as a non-empty marker rather than collapsing to '' and being
        skipped by the falsy guard."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=[IMAGE_PART, IMAGE_PART],
            final_response="Reply: skip.",
            interrupted=False,
        )
        user_arg, _ = _sync_args(agent)
        assert user_arg == "[2 images]"

    def test_multimodal_assistant_response_is_flattened(self):
        """``final_response`` reaches the provider on the same call and is
        equally capable of arriving as a part list."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message="text only",
            final_response=[{"type": "text", "text": "Reply: approve."}],
            interrupted=False,
        )
        _, assistant_arg = _sync_args(agent)
        assert assistant_arg == "Reply: approve."

    def test_prefetch_query_is_flattened_too(self):
        """``queue_prefetch_all`` feeds the same value into provider search
        APIs, so it needs the identical treatment."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=[TEXT_PART, IMAGE_PART],
            final_response="Reply: approve.",
            interrupted=False,
        )
        agent._memory_manager.queue_prefetch_all.assert_called_once()
        args, _ = agent._memory_manager.queue_prefetch_all.call_args
        assert isinstance(args[0], str)
        assert args[0] == "[1 image] Evaluate this story"

    # --- Existing behaviour preserved -----------------------------------

    def test_plain_string_turn_is_unchanged(self):
        """Flattening a string returns it identically, so the ordinary
        text path must be byte-for-byte what it was before the fix."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message="What's the weather in Paris?",
            final_response="It's sunny and 22°C.",
            interrupted=False,
        )
        agent._memory_manager.sync_all.assert_called_once_with(
            "What's the weather in Paris?", "It's sunny and 22°C.",
            session_id="test_session_001",
        )

    def test_raw_messages_kwarg_is_forwarded_untouched(self):
        """Only the two summary arguments are flattened.  ``messages`` is
        the structured transcript providers parse themselves, so it must
        pass through byte-identical."""
        agent = _bare_agent()
        messages = [{"role": "user", "content": [TEXT_PART, IMAGE_PART]}]
        agent._sync_external_memory_for_turn(
            original_user_message=[TEXT_PART, IMAGE_PART],
            final_response="Reply: approve.",
            interrupted=False,
            messages=messages,
        )
        _, kwargs = agent._memory_manager.sync_all.call_args
        assert kwargs["messages"] is messages

    @pytest.mark.parametrize("empty", [[], [{"type": "image_url"}][:0]])
    def test_empty_part_list_skips_sync(self, empty):
        """An empty list flattens to '' — that is not durable truth, so it
        must be skipped rather than written as a blank memory."""
        agent = _bare_agent()
        agent._sync_external_memory_for_turn(
            original_user_message=empty,
            final_response="Reply: approve.",
            interrupted=False,
        )
        agent._memory_manager.sync_all.assert_not_called()
