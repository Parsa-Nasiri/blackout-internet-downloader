"""Update parsing tests — these mirror the shapes in rubika.ir/botapi/models."""

from rubika_dl.updates import parse_inline_message, parse_update

NEW_MESSAGE = {
    "type": "NewMessage",
    "chat_id": "1000",
    "new_message": {
        "message_id": "55",
        "text": "/vid https://youtu.be/x",
        "time": 1700000000,
        "is_edited": False,
        "sender_type": "User",
        "sender_id": "42",
        "aux_data": {"start_id": "", "button_id": ""},
    },
}


def test_parses_new_message():
    event = parse_update(NEW_MESSAGE)
    assert event is not None
    assert event.chat_id == "1000"
    assert event.message_id == "55"
    assert event.sender_id == "42"
    assert event.text == "/vid https://youtu.be/x"


def test_command_parsing():
    event = parse_update(NEW_MESSAGE)
    assert event.is_command
    assert event.command == "vid"
    assert event.args == "https://youtu.be/x"
    assert event.urls == ["https://youtu.be/x"]


def test_command_with_bot_suffix():
    update = {
        "type": "NewMessage",
        "chat_id": "1",
        "new_message": {"message_id": "1", "text": "/help@MyBot please", "sender_id": "9"},
    }
    event = parse_update(update)
    assert event.command == "help"
    assert event.args == "please"


def test_button_click_carries_aux_data():
    update = {
        "type": "NewMessage",
        "chat_id": "1000",
        "new_message": {
            "message_id": "77",
            "text": "",
            "sender_id": "42",
            "aux_data": {"start_id": "abc", "button_id": "tok123|1080p"},
        },
    }
    event = parse_update(update)
    assert event.button_id == "tok123|1080p"
    assert event.start_id == "abc"


def test_removed_message():
    event = parse_update({"type": "RemovedMessage", "chat_id": "1", "removed_message_id": "88"})
    assert event.message_id == "88"
    assert event.update_type == "RemovedMessage"


def test_started_bot_maps_to_start():
    event = parse_update({
        "type": "StartedBot",
        "chat_id": "1000",
        "aux_data": {"start_id": "xyz", "sender_id": "42"},
    })
    assert event.text == "/start"
    assert event.start_id == "xyz"


def test_message_with_file_attachment():
    update = {
        "type": "NewMessage",
        "chat_id": "1",
        "new_message": {
            "message_id": "5",
            "text": "",
            "sender_id": "42",
            "file": {"file_id": "f1", "file_name": "a.mp4", "size": "1234"},
        },
    }
    event = parse_update(update)
    assert event.has_media_attachment
    assert event.attachment.file_name == "a.mp4"
    assert event.attachment.size == 1234


def test_inline_message():
    event = parse_inline_message({
        "inline_message": {
            "sender_id": "42",
            "text": "",
            "message_id": "77",
            "chat_id": "1000",
            "aux_data": {"start_id": "", "button_id": "canceljob|abc"},
        }
    })
    assert event.is_inline
    assert event.button_id == "canceljob|abc"
    assert event.chat_id == "1000"


def test_unknown_update_returns_none():
    assert parse_update({"type": "SomethingNew", "chat_id": "1"}) is None


def test_group_detection():
    update = {
        "type": "NewMessage",
        "chat_id": "-100",
        "new_message": {"message_id": "1", "text": "hi", "sender_id": "5", "chat_type": "Group"},
    }
    event = parse_update(update)
    assert event.is_group
    assert not event.is_private
