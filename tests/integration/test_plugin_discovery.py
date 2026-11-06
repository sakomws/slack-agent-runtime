from pathlib import Path

from slack_agent_runtime.plugins import ENTRY_POINT_GROUP, load_plugin_registry

PROJECT_ROOT = Path(__file__).parents[2]


def test_echo_plugin_is_discovered_from_distribution_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise importlib discovery rather than replacing it with a fake loader."""

    distribution = tmp_path / "slack_agent_runtime_echo-0.2.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: slack-agent-runtime-echo\nVersion: 0.2.0\n",
        encoding="utf-8",
    )
    (distribution / "entry_points.txt").write_text(
        f"[{ENTRY_POINT_GROUP}]\necho = slack_agent_runtime_echo.plugin:create_plugin\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "examples" / "echo_plugin" / "src"))
    monkeypatch.syspath_prepend(str(tmp_path))

    registry = load_plugin_registry(["echo"])

    assert tuple(plugin.name for plugin in registry.plugins) == ("echo",)
