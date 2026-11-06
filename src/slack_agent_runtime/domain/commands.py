"""Command parsing and validation."""

import math
from dataclasses import dataclass
from enum import StrEnum

REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class CommandKind(StrEnum):
    HELP = "help"
    CANCEL = "cancel"
    CLEAR = "clear"
    DELETE_LAST = "delete_last"
    REGENERATE = "regenerate"
    MODEL = "model"
    TOGGLE = "toggle"
    PARAMETER = "parameter"
    SETTINGS = "settings"
    SETTINGS_DEFAULT = "settings_default"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Describe parsing, Slack routing, and help for one command."""

    kind: CommandKind
    phrases: tuple[str, ...]
    help_group: str
    help_usage: str | None
    name: str | None = None
    accepts_value: bool = False
    help_description: str | None = None

    @property
    def route_identifiers(self) -> tuple[str, ...]:
        return tuple(phrase.partition(" ")[0] for phrase in self.phrases)


@dataclass(frozen=True, slots=True)
class HelpEntry:
    group: str
    usage: str
    description: str | None = None


COMMAND_SPECS = (
    CommandSpec(
        CommandKind.MODEL,
        ("model",),
        "Models & search",
        "model [name|default]",
        accepts_value=True,
        help_description="show or select the inference model",
    ),
    CommandSpec(
        CommandKind.TOGGLE,
        ("web",),
        "Models & search",
        "web [on|off]",
        name="web",
        accepts_value=True,
        help_description="show or toggle web search",
    ),
    CommandSpec(
        CommandKind.TOGGLE,
        ("usage",),
        "Models & search",
        "usage [on|off]",
        name="usage",
        accepts_value=True,
        help_description="show or toggle request and token details",
    ),
    CommandSpec(
        CommandKind.TOGGLE,
        ("stream",),
        "Models & search",
        "stream [on|off]",
        name="stream",
        accepts_value=True,
        help_description="show or toggle incremental responses",
    ),
    CommandSpec(
        CommandKind.PARAMETER,
        ("temperature",),
        "Generation",
        "temperature [0-2|default]",
        name="temperature",
        accepts_value=True,
        help_description="show or set response randomness",
    ),
    CommandSpec(
        CommandKind.PARAMETER,
        ("top-p",),
        "Generation",
        "top-p [0-1|default]",
        name="top-p",
        accepts_value=True,
        help_description="show or set nucleus sampling",
    ),
    CommandSpec(
        CommandKind.PARAMETER,
        ("max-tokens",),
        "Generation",
        "max-tokens [1-1000000|default]",
        name="max-tokens",
        accepts_value=True,
        help_description="show or set the response limit in tokens",
    ),
    CommandSpec(
        CommandKind.PARAMETER,
        ("reasoning",),
        "Generation",
        "reasoning [level|default]",
        name="reasoning",
        accepts_value=True,
        help_description="show or set reasoning effort",
    ),
    CommandSpec(
        CommandKind.REGENERATE,
        ("regenerate",),
        "Thread actions",
        "regenerate",
        help_description="rerun the latest request",
    ),
    CommandSpec(
        CommandKind.CANCEL,
        ("cancel",),
        "Thread actions",
        "cancel",
        help_description="stop the active request",
    ),
    CommandSpec(
        CommandKind.DELETE_LAST,
        ("delete last",),
        "Thread actions",
        "delete last",
        help_description="remove the latest assistant response",
    ),
    CommandSpec(
        CommandKind.CLEAR,
        ("clear", "reset"),
        "Thread actions",
        "clear",
        help_description="clear this thread's history and settings",
    ),
    CommandSpec(
        CommandKind.SETTINGS,
        ("settings",),
        "Settings & info",
        "settings",
        help_description="show current thread settings",
    ),
    CommandSpec(
        CommandKind.SETTINGS_DEFAULT,
        ("settings default",),
        "Settings & info",
        "settings default",
        help_description="reset settings while preserving history",
    ),
    CommandSpec(
        CommandKind.HELP,
        ("help",),
        "Settings & info",
        "help",
        help_description="show available commands",
    ),
)

COMMAND_IDENTIFIERS = tuple(
    dict.fromkeys(
        identifier
        for specification in COMMAND_SPECS
        for identifier in specification.route_identifiers
    )
)


def build_help_text(additional: tuple[HelpEntry, ...] = ()) -> str:
    sections = ["*Slack Agent Runtime*"]
    entries = (
        *(
            HelpEntry(
                specification.help_group,
                specification.help_usage,
                specification.help_description,
            )
            for specification in COMMAND_SPECS
            if specification.help_usage is not None
        ),
        *additional,
    )
    groups = dict.fromkeys(entry.group for entry in entries)
    for group in groups:
        group_entries = [
            f"• `{entry.usage}`"
            + (f" — {entry.description}" if entry.description is not None else "")
            for entry in entries
            if entry.group == group
        ]
        sections.append(f"*{group}*\n" + "\n".join(group_entries))
    sections.append("_All settings are per thread. `default` removes an override._")
    return "\n\n".join(sections)


HELP_TEXT = build_help_text()


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    name: str | None = None
    value: str | None = None


def parse_command(text: str) -> Command | None:
    words = text.split()
    if not words:
        return None
    normalized = " ".join(word.lower() for word in words)
    for specification in COMMAND_SPECS:
        if not specification.accepts_value:
            if normalized in specification.phrases:
                return Command(specification.kind, name=specification.name)
            continue
        if words[0].lower() not in specification.phrases or len(words) > 2:
            continue
        value = words[1] if len(words) == 2 else None
        if (
            specification.kind is CommandKind.TOGGLE
            and value is not None
            and value.lower() not in {"on", "off"}
        ):
            return None
        return Command(specification.kind, name=specification.name, value=value)
    return None


def parse_parameter(name: str, raw_value: str) -> tuple[str | int | float | None, str | None]:
    if name == "reasoning":
        value = raw_value.lower()
        if value in REASONING_LEVELS:
            return value, None
        return None, "Reasoning must be one of: " + ", ".join(
            f"`{level}`" for level in REASONING_LEVELS
        ) + "."
    if name == "max-tokens":
        try:
            integer_value = int(raw_value)
        except ValueError:
            integer_value = 0
        return (
            (integer_value, None)
            if 1 <= integer_value <= 1_000_000
            else (None, "Max tokens must be an integer from 1 to 1,000,000.")
        )
    try:
        numeric_value = float(raw_value)
    except ValueError:
        numeric_value = math.nan
    upper = 2.0 if name == "temperature" else 1.0
    if math.isfinite(numeric_value) and 0 <= numeric_value <= upper:
        return numeric_value, None
    return None, f"`{name}` must be a number from 0 to {upper:g}."
