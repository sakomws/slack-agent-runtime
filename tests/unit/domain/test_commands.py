from slack_agent_runtime.domain.commands import (
    CommandKind,
    HelpEntry,
    build_help_text,
    parse_command,
    parse_parameter,
)


def test_parse_command_normalizes_simple_commands() -> None:
    command = parse_command("  settings   default ")

    assert command is not None
    assert command.kind is CommandKind.SETTINGS_DEFAULT


def test_parse_command_preserves_model_alias() -> None:
    command = parse_command("model example-model")

    assert command is not None
    assert command.kind is CommandKind.MODEL
    assert command.value == "example-model"


def test_parse_command_rejects_invalid_toggle() -> None:
    assert parse_command("web maybe") is None


def test_parse_parameter_validates_bounds() -> None:
    assert parse_parameter("temperature", "0.7") == (0.7, None)
    value, error = parse_parameter("temperature", "3")
    assert value is None
    assert error == "`temperature` must be a number from 0 to 2."


def test_help_describes_built_in_and_plugin_commands() -> None:
    text = build_help_text(
        (HelpEntry("MCP tools", "tools", "list tools and their approval behavior"),)
    )

    assert "`model [name|default]` — show or select the inference model" in text
    assert "`clear` — clear this thread's history and settings" in text
    assert "`tools` — list tools and their approval behavior" in text
