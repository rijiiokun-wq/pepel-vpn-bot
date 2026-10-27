from types import SimpleNamespace

import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import handlers


class _FakeSourceMessage:
    chat = SimpleNamespace(id="777")
    message_id = "555"
    html_text = "<b>hello</b>"
    text = "hello"
    html_caption = None
    caption = None

    def model_dump_json(self):
        return '{"message_id": 555}'


class _RichTextMessageWithBrokenSerialization:
    chat = SimpleNamespace(id="777")
    message_id = "555"
    text = "plain pasted text"
    caption = "plain pasted caption"

    @property
    def html_text(self):
        msg = "html text renderer failed"
        raise ValueError(msg)

    @property
    def html_caption(self):
        msg = "html caption renderer failed"
        raise ValueError(msg)

    def model_dump_json(self):
        msg = "telegram payload serialization failed"
        raise ValueError(msg)


def test_broadcast_source_data_stores_stable_message_reference():
    payload = handlers._build_broadcast_source_data(_FakeSourceMessage())

    assert payload["broadcast_source_chat_id"] == "777"
    assert payload["broadcast_source_message_id"] == "555"
    assert payload["broadcast_text_fallback"] == "<b>hello</b>"
    assert payload["message_to_send"] == '{"message_id": 555}'


def test_broadcast_source_ref_prefers_stable_ids_over_message_json():
    source_chat_id, source_message_id = handlers._resolve_broadcast_source_ref(
        {
            "broadcast_source_chat_id": "777",
            "broadcast_source_message_id": "555",
            "message_to_send": "not-json",
        }
    )

    assert source_chat_id == 777
    assert source_message_id == 555


def test_broadcast_source_data_keeps_buttons_available_for_rich_pasted_messages():
    payload = handlers._build_broadcast_source_data(_RichTextMessageWithBrokenSerialization())

    assert payload["broadcast_source_chat_id"] == "777"
    assert payload["broadcast_source_message_id"] == "555"
    assert payload["message_to_send"] == ""
    assert payload["broadcast_text_fallback"] == "plain pasted text"
    assert payload["broadcast_caption_fallback"] == "plain pasted caption"
