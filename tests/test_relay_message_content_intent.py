import logging

from test_relay_routing import (  # noqa: F401
    CH, Msg, adapter, deliver, mention_b, relay, workspace,
)


def test_empty_content_with_mentions_logs_intent_error_once(adapter, caplog):
    with caplog.at_level(logging.ERROR):
        deliver(adapter, Msg(CH["general"], "", mentions=mention_b()))
        deliver(adapter, Msg(CH["general"], "", mentions=mention_b()))
    assert caplog.text.count("Message Content intent is OFF") == 1


def test_normal_message_does_not_log(adapter, caplog):
    with caplog.at_level(logging.ERROR):
        deliver(adapter, Msg(CH["general"], "hello"))
    assert "Message Content intent" not in caplog.text


def test_empty_content_without_mentions_does_not_log(adapter, caplog):
    with caplog.at_level(logging.ERROR):
        deliver(adapter, Msg(CH["general"], ""))
    assert caplog.text == ""


def test_error_text_names_the_portal_path(relay):
    assert "Privileged Gateway Intents -> Message Content Intent" in relay.INTENT_ERROR
