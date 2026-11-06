from slack_agent_runtime.slack.formatting import (
    split_slack_message,
    to_slack_mrkdwn,
)


def test_markdown_citations_become_slack_links_and_mentions_are_escaped() -> None:
    text = "See [[1]](https://example.com/a?q=1), [the source](https://example.com/b), and <@U123>."

    assert to_slack_mrkdwn(text) == (
        "See <https://example.com/a?q=1|[1]>, "
        "<https://example.com/b|the source>, and &lt;@U123&gt;."
    )


def test_markdown_headings_emphasis_and_plain_citations_become_readable() -> None:
    text = """## Today's big announcement

Google announced **Guided Learning** with *new controls* [[1]][[9]].

### Other developments

This is ~~old~~ current."""

    assert (
        to_slack_mrkdwn(text)
        == """*Today's big announcement*

Google announced *Guided Learning* with _new controls_ [1][9].

*Other developments*

This is ~old~ current."""
    )


def test_code_formatting_is_not_rewritten() -> None:
    text = "Use `**literal**`:\n```\n## not a heading\n<@U123>\n```"

    assert to_slack_mrkdwn(text) == (
        "Use `**literal**`:\n```\n## not a heading\n&lt;@U123&gt;\n```"
    )


def test_long_messages_split_on_readable_boundaries() -> None:
    chunks = split_slack_message("first paragraph\n\nsecond paragraph", limit=20)

    assert chunks == ["first paragraph", "second paragraph"]
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_long_unbroken_text_is_hard_split() -> None:
    chunks = split_slack_message("x" * 25, limit=10)

    assert chunks == ["x" * 10, "x" * 10, "x" * 5]
