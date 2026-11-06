# Give the Bot the Keyboard, Not the Keys

> **Slack Agent Runtime:** from a Slack thread to a reviewed pull request, and
> the AI never holds a GitHub or Slack token.

A 30-minute talk for software engineers and platform teams. One idea carries
it: **the agent gets to type, it never gets to publish.** Slack is the harness,
MCP is the data layer, one OpenAI-compatible endpoint (You.com in the current
deployment) does inference and search, GitHub Actions is the bounded sandbox,
and GitHub holds the artifacts. Everything else is how we keep the keys away
from the model.

## Run of show

| # | Slide | Time |
| ---: | --- | ---: |
| 1 | Give the bot the keyboard. Not the keys. | 0.5 min |
| 2 | Friday, 4:52 pm | 1.5 min |
| 3 | Chat is easy. Acting is where it bites. | 2 min |
| 4 | Five layers. One job each. | 2 min |
| 5 | Follow one request through the system | 3 min |
| 6 | The model never holds the keys. | 2 min |
| 7 | Bounded means finite, not instant. | 2 min |
| 8 | Off-limits while it works. Checked after it’s gone. | 2 min |
| 9 | Read-only is earned, not assumed. | 2.5 min |
| 10 | Missing facts stay missing. | 1.5 min |
| 11 | The model finds. The code decides. | 2.5 min |
| 12 | A restart can lose a chat. Not a job. | 2 min |
| 13 | Live demo: five commands, one thread | 2 min |
| 14 | Three ideas worth stealing | 1 min |
| 15 | Before you ship it | 0.5 min |
| 16 | Keyboard: yes. Keys: never. | 0.5 min |
| | Buffer for demo slippage | 2.5 min |
| | **Total** | **30 min** |

Q&A runs after the 30 minutes.

---

## 1 · Give the bot the keyboard. Not the keys.

**On screen:** the title, the one-line promise, the repo URL.

Let the title sit for a beat. The whole talk is that line: the agent can write
code, it can never publish it. The rest is how we enforce that.

---

## 2 · Friday, 4:52 pm

**On screen:** a Slack thread.

```text
you    code sakomws/everyballwins Add validation for ball creation
agent  Job queued in GitHub Actions… done. Pull request opened on a fresh
       agent/ branch, ready for your review.
```

> The model wrote the change. **It never saw a GitHub token.**

Open with the experience, not the architecture. One line in a thread comes back
as a pull request a human reviews. Then the twist: the AI that wrote the code
never held the credential that published it. The next 28 minutes are how.

---

## 3 · Chat is easy. Acting is where it bites.

**On screen:** four cards.

- **Threads are concurrent:** channels, threads, users, cancels and retries,
  all at once.
- **Tools aren’t harmless:** an MCP server runs with your runtime’s permissions.
- **Jobs outlive processes:** a coding run takes minutes and must survive a bot
  restart.
- **Models leak what they hold:** any credential in the model’s process is one
  prompt injection away.

A bot that forwards prompts to a model is a weekend project. The moment it
acts, all four bite. Tool names, arguments, output size, timeouts and approvals
need policy. Long jobs belong in a durable job system, not an in-memory task.
And anything the model’s process can read, a malicious diff or page can try to
make it leak.

---

## 4 · Five layers. One job each.

| Layer | Job | What it means here |
| --- | --- | --- |
| **Slack** | The harness | Who asked, where, and who approves |
| **MCP** | The data layer | Notion specs, Linear work items, Slack data |
| **You.com** | The brain | One OpenAI-compatible endpoint for inference and search |
| **GitHub Actions** | The sandbox | Bounded, durable workers |
| **GitHub** | The artifacts | Branches, diffs, PRs, reviews |

Slack arrives over outbound Socket Mode, so there is no public webhook. The
reusable runtime is provider-neutral; You.com is simply this deployment’s single
endpoint. Human merge approval and branch protection stay the final control.

---

## 5 · Follow one request through the system

![Slack Engineering Agent Architecture](images/slack-engineering-agent-architecture.svg)

Walk three paths.

- **Conversation:** Slack event over Socket Mode → bounded thread history →
  the endpoint’s OpenAI-compatible API → throttled progress edits → only the
  final answer is stored.
- **Data:** MCP runs inside the runtime, because that is where approvals and
  source credentials live. MCP is not a sandbox; install only trusted servers.
- **Code:** `repository_dispatch` leaves the runtime and starts a workflow.
  Actions becomes the durable source of job status.

Point at the yellow pills: they are the only places credentials appear, and
the GitHub and Slack ones sit *after* the worker exits.

---

## 6 · The model never holds the keys.

**On screen:** the rule, then four steps.

1. **Worker runs** with an inference endpoint, key and model. Nothing else.
2. **Worker exits.** Its process is gone before any token appears.
3. **Diff is checked** in plain code: paths, metadata, secrets.
4. **Keys appear:** GitHub, then Slack, each in its own later step.

The target repo is checked out without persisted credentials. “Later step”
means a separate GitHub Actions step that starts after the AI worker process
has finished, not a later tool call in the same process.

> A prompt injection can make the model say anything. It can’t make it leak a
> token it never had.

---

## 7 · Bounded means finite, not instant.

| 48 | 20 | 50 | 500K | 30 |
| :---: | :---: | :---: | :---: | :---: |
| model/tool rounds | commands | changed files | diff characters | minute timeout |

All configurable defaults
([worker.py](../src/slack_agent_runtime/coding/worker.py),
[coding_worker.py](../src/slack_agent_runtime/entrypoints/coding_worker.py)).
Rounds are sequential and tests can take minutes, so bounded never means fast;
it means the worst case has a ceiling.

Near the end of its budget the model is told to wrap up. If it still runs out,
the runtime checks for a valid diff: none is a failure; a valid one switches
tools off and asks for one factual summary that calls out unfinished work.

---

## 8 · Off-limits while it works. Checked after it’s gone.

| The worker can’t touch | The workflow rejects |
| --- | --- |
| `.git` and `.github/workflows` | Changes to protected paths |
| Environment files and secret folders | An empty diff |
| Private keys | Secret-looking worker metadata |
| A shell: commands are argument arrays from an allowlist | Anything else: it’s a PR, and a human merges it |

The branch is named `agent/` plus the job ID, and generated PRs are never
auto-merged.

**Say the caveat out loud:** this contains the agent; it does not make hostile
repository code safe to run. Allowed tests execute repository code, so use it
on trusted repositories.

---

## 9 · Read-only is earned, not assumed.

> **Operator allowlisted the tool** + **server marks it `readOnlyHint`** =
> **runs automatically.**
>
> Anything else, including missing metadata: a Slack admin approves the call
> while it waits.

The runtime connects straight to configured MCP servers over stdio or
Streamable HTTP; no gateway. Tool names are namespaced per server; rounds,
timeouts and output size are capped; arguments and results are never logged.

Approval waits live in memory, which is why the docs say one active runtime
replica when approvals are on. And MCP servers are trusted code, not a sandbox.

---

## 10 · Missing facts stay missing.

```text
spec review from notion <page URL>
```

- Only the Notion MCP server’s tools
- At least one successful source call
- Facts, assumptions, decisions and open questions kept apart
- No owner or deadline? It’s an open question, never a guess

Specs are retrieved before any code is dispatched, and Notion credentials stay
in the Slack runtime; they never enter GitHub Actions. The review returns a
revised spec and does not start a coding job; that is a separate,
Notion-backed command.

---

## 11 · The model finds. The code decides.

1. Pin the PR’s head commit SHA.
2. Treat every title, body, comment and diff as untrusted.
3. The model returns strict JSON findings.
4. Recheck the SHA and required checks, then publish.

| Findings | Verdict, set in code |
| --- | --- |
| Critical or high | Request changes |
| Medium, low or none | Approve, if policy allows |
| Draft, failing checks, or the PR author’s own token | Comment only |

**And it never merges.** The runtime sanitizes the findings and computes the
blocking flag itself; the model never chooses the GitHub event. Findings render
as stacked sections (severity, location, issue, recommendation), not a cramped
table.

---

## 12 · A restart can lose a chat. Not a job.

| Survives a restart | Lost on purpose |
| --- | --- |
| Thread snapshots in SQLite, Redis or PostgreSQL | Active requests and cancellations |
| History rebuilt from Slack replies | Locks, rate limits, token budgets |
| Coding and review status in GitHub Actions | Pending MCP approvals |

So: **one active Socket Mode runtime per Slack app.** Socket Mode spreads
events across connections, so replicas aren’t stateless HTTP replicas.

Deploy locally, with Docker Compose, or on Kubernetes with Helm. Telemetry uses
standard OTLP settings and records model, duration, usage and error type, never
prompts, completions or MCP payloads. Cost estimates are opt-in, from a pricing
map you supply.

---

## 13 · Live demo: five commands, one thread

```text
help
prs sakomws/goup.vc
review sakomws/goup.vc#192
code sakomws/everyballwins Add validation for ball creation
code status
```

Recheck these repositories and the PR right before presenting.

1. `help`: point out the MCP, coding, spec and PR categories.
2. `prs`: bounded listing with review buttons.
3. Show an existing generated review with stacked findings.
4. Run the narrow coding request, then `code status`.
5. Open the Actions run and find the handoff: uncredentialed worker first,
   then the GitHub and Slack steps.

If Actions is slow, switch to a completed run. Show `spec review` only if a
real, accessible Notion page is ready; never a made-up URL.

---

## 14 · Three ideas worth stealing

1. Slack is an engineering harness, not just a chat window.
2. Keep the thinking away from the keys that publish.
3. Data to MCP. Execution to Actions. Artifacts to GitHub.

If people remember one thing, it is number two.

---

## 15 · Before you ship it

| Owner | When | Do this |
| --- | --- | --- |
| Program sponsor | Before scheduling | Name real people and dates for every role |
| Platform owner | Before rollout | Pin reusable workflows to a release or commit SHA |
| Repository owner | Before enabling | Branch protection, encrypted secrets, exact repo allowlist |
| Security owner | Before MCP is on | Approve each MCP server, the read-only tool allowlist and the command allowlist |
| Operations owner | Before launch | Monitoring, key rotation, rollback, incident response |

This talk deliberately doesn’t invent owners or dates; the adopting team fills
them in.

---

## 16 · Keyboard: yes. Keys: never.

Close on the callback to the title, then open Q&A.

### Q&A prompts

- Why GitHub Actions instead of running code inside the Slack process?
- What survives a restart, and what is intentionally lost?
- What stops a malicious diff from steering the reviewer?
- Why is MCP trusted code rather than a sandbox?
- How would you add another source, such as Google Drive?
- What extra isolation would hostile repositories need?

---

## Presenter checklist

- **One business day before:** verify Slack, model, repository and Actions
  access, and record a completed run as a fallback.
- **Assumed audience:** comfortable with Slack, GitHub pull requests and HTTP
  APIs.
- **Facts:** every architecture, command and security claim comes from the
  files below. Any OpenAI-compatible endpoint works; You.com is named because it
  is the current deployment’s single endpoint for inference and search.

## Repository references

- [README](../README.md)
- [Contributor architecture and security boundaries](../AGENTS.md)
- [Slack bot assembly](../src/slack_agent_runtime/slack/bot.py)
- [Plugin contracts](../src/slack_agent_runtime/plugins/api.py)
- [MCP manager](../src/slack_agent_runtime/mcp/manager.py)
- [Coding worker](../src/slack_agent_runtime/coding/worker.py)
- [Workspace tools](../src/slack_agent_runtime/coding/tools.py)
- [Pull-request reviewer](../src/slack_agent_runtime/coding/reviewer.py)
- [Reusable coding workflow](../.github/workflows/coding-job-reusable.yml)
- [Reusable review workflow](../.github/workflows/pr-review-reusable.yml)
