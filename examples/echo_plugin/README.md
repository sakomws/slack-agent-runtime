# Echo plugin

This minimal external plugin adds an `echo [text]` command and stores a
per-thread invocation count in the bot's configured state backend.

From the bot repository:

```bash
pip install -e .
pip install -e ./examples/echo_plugin
cp .env.example .env
```

Set `SLACK_AGENT_RUNTIME_PLUGINS=echo` in `.env`, then run:

```bash
slack-agent --check
slack-agent
```

Send `echo hello` to the bot in Slack. The response is `Echo: hello`.

The package registers a zero-argument factory in
`pyproject.toml`. The factory returns `PluginDefinition`; its command handler
uses only documented context values and the service's namespaced plugin-state
methods. Uninstalling the package or removing `echo` from `SLACK_AGENT_RUNTIME_PLUGINS`
disables it without changing the core bot.
