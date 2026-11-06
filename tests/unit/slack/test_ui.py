from slack_agent_runtime.slack.ui import (
    ACTION_CANCEL,
    ACTION_CLEAR,
    ACTION_FEEDBACK_NEGATIVE,
    ACTION_FEEDBACK_POSITIVE,
    ACTION_MODEL_OPEN,
    ACTION_REGENERATE,
    ACTION_STREAM_TOGGLE,
    ACTION_WEB_TOGGLE,
    model_modal,
    model_selection,
    progress_blocks,
    response_blocks,
)


def test_response_blocks_expose_all_completed_response_actions() -> None:
    blocks = response_blocks("Answer", web_enabled=True, streaming_enabled=True)
    action_ids = {element["action_id"] for block in blocks for element in block.get("elements", [])}

    assert action_ids == {
        ACTION_REGENERATE,
        ACTION_WEB_TOGGLE,
        ACTION_STREAM_TOGGLE,
        ACTION_MODEL_OPEN,
        ACTION_CLEAR,
        ACTION_FEEDBACK_POSITIVE,
        ACTION_FEEDBACK_NEGATIVE,
    }


def test_progress_blocks_offer_cancel() -> None:
    blocks = progress_blocks("Thinking…")

    assert blocks[1]["elements"][0]["action_id"] == ACTION_CANCEL


def test_model_modal_round_trips_thread_context_and_selection() -> None:
    view = model_modal(
        ("default-model", "example-model"),
        current_model="default-model",
        channel="C123",
        thread_ts="100.000",
    )
    view["state"] = {
        "values": {
            "model": {
                "assistant.model_select": {
                    "selected_option": {"value": "example-model"},
                }
            }
        }
    }

    assert model_selection(view) == ("example-model", "C123", "100.000")


def test_model_selection_rejects_tampered_metadata() -> None:
    assert model_selection({"private_metadata": "not-json"}) is None
