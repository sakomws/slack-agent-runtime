# MCP server examples

Copy one template to the repository root as the ignored `mcp.json`, add its
environment variables to `.env`, and set:

```dotenv
SLACK_AGENT_RUNTIME_MCP_CONFIG=mcp.json
```

## GitHub

```bash
cp examples/mcp/github.json mcp.json
```

Set `GITHUB_MCP_TOKEN` to a narrowly scoped token. The template connects to
GitHub's hosted Streamable HTTP MCP endpoint.

## Notion

```bash
cp examples/mcp/notion.json mcp.json
```

Install Node.js so `npx` is available. Set `NOTION_MCP_HEADERS` to the JSON
header object required by the Notion MCP server. Keep the integration token
scoped only to pages the agent should access.

When the GitHub coding layer is also enabled, an app administrator can retrieve
a spec and dispatch it in one approved flow:

```text
code owner/repository from notion https://www.notion.so/example-spec
```

The configured server must be named `notion`, as in the supplied template.
Notion credentials remain in this runtime; only the bounded, normalized
requirements are sent to the target repository's Actions workflow.

## Linear

```bash
cp examples/mcp/linear.json mcp.json
```

Set `LINEAR_MCP_TOKEN` to a Linear API key or OAuth access token with read-only
access. The template uses Linear's official hosted Streamable HTTP read-only
endpoint. Keep `autoApprove` empty until each discovered tool and its
annotations have been reviewed.

## Slack

```bash
cp examples/mcp/slack.json mcp.json
```

Install Node.js and set `SLACK_MCP_BOT_TOKEN` plus `SLACK_MCP_TEAM_ID`. Use a
separate, least-privilege Slack app for MCP tool access rather than reusing the
runtime's own bot token.

## You.com

```bash
cp examples/mcp/youdotcom.json mcp.json
```

The template connects directly to You.com's official keyless Streamable HTTP
endpoint. It exposes `you-search` and `you-discover` with a free daily query
limit and requires no additional credential. For production or additional
tools, use `https://api.you.com/mcp` with a dedicated `YDC_API_KEY` bearer
token.

To enable several servers, merge their entries under one `mcpServers` object.
`autoApprove` is empty in every template: tools require administrator approval
until an operator explicitly allowlists a tool that also advertises
`readOnlyHint`.
