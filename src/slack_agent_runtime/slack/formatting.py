import re

SLACK_MESSAGE_CHUNK_CHARS = 2_900
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_CITATION_LINK = re.compile(r"\[\[([0-9,\s]+)\]\]\((https?://[^\s)]+)\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD_ASTERISKS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDERSCORES = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_ITALIC_ASTERISKS = re.compile(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)")
_STRIKETHROUGH = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_PLAIN_CITATION = re.compile(r"\[\[([0-9,\s]+)\]\]")


def to_slack_mrkdwn(text: str) -> str:
    """Convert common Markdown to Slack mrkdwn and prevent generated mentions."""
    protected: list[str] = []

    def stash(value: str) -> str:
        index = len(protected)
        protected.append(value)
        return f"\x00SLACK_FORMAT_{index}\x00"

    def replace_code(match: re.Match[str]) -> str:
        return stash(_escape(match.group(0)))

    def replace_citation_link(match: re.Match[str]) -> str:
        label = _escape(match.group(1).strip())
        url = _escape(match.group(2))
        return stash(f"<{url}|[{label}]>")

    def replace_link(match: re.Match[str]) -> str:
        label = _escape(match.group(1))
        url = _escape(match.group(2))
        return stash(f"<{url}|{label}>")

    formatted = _CODE.sub(replace_code, text)
    formatted = _CITATION_LINK.sub(replace_citation_link, formatted)
    formatted = _MARKDOWN_LINK.sub(replace_link, formatted)
    formatted = _escape(formatted)
    formatted = _ITALIC_ASTERISKS.sub(r"_\1_", formatted)
    formatted = _BOLD_ASTERISKS.sub(r"*\1*", formatted)
    formatted = _BOLD_UNDERSCORES.sub(r"*\1*", formatted)
    formatted = _STRIKETHROUGH.sub(r"~\1~", formatted)
    formatted = _PLAIN_CITATION.sub(r"[\1]", formatted)
    formatted = _HEADING.sub(r"*\1*", formatted)

    for index, value in enumerate(protected):
        formatted = formatted.replace(f"\x00SLACK_FORMAT_{index}\x00", value)
    return formatted


def split_slack_message(
    text: str,
    *,
    limit: int = SLACK_MESSAGE_CHUNK_CHARS,
) -> list[str]:
    if not text:
        return []

    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = _split_position(remaining, limit)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_position(text: str, limit: int) -> int:
    minimum = limit // 2
    for separator in ("\n\n", "\n", " "):
        position = text.rfind(separator, minimum, limit + 1)
        if position >= minimum:
            return position + len(separator)
    return limit


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
