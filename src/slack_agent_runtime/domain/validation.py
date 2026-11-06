"""Shared validation rules."""

import re

PLUGIN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def is_plugin_name(value: object) -> bool:
    return isinstance(value, str) and PLUGIN_NAME_PATTERN.fullmatch(value) is not None
