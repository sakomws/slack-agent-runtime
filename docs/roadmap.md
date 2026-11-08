# Slack Agent Runtime Roadmap

This roadmap is for maintainers and contributors deciding what to build after
version `0.1.0`. Its purpose is to turn recurring agent failures into enforced
harness behavior. The immediate recommendation is to prioritize deterministic
completion evidence for coding jobs before adding more autonomy or execution
surface.

**Status:** Proposed

**Owner:** Not assigned

**Last updated:** November 8, 2026

## Guiding principle

The model proposes actions. The harness decides what is allowed, records what
happened, and determines whether the work is complete.

This direction is informed by Trackmind's
[Harness Guide for Agents That Actually Ship](https://x.com/0xTrackmind/status/2113644871529418857).
The article's useful design themes are task contracts, concise repository maps,
evidence-based completion, external permission enforcement, bounded retries,
durable state, and converting repeated failures into automated checks. Its
reported measurements are not treated as evidence about this project.

## Existing foundation

Slack Agent Runtime already implements several of those harness controls:

- GitHub repository allowlists and human-reviewed pull requests.
- Shell-free, allowlisted command execution with time, output, file, diff, and
  command limits.
- Protected environment, secret, workflow, private-key, and Git paths.
- Separation between inference, GitHub, and Slack credentials.
- Durable Slack thread state and durable pointers to GitHub Actions jobs.
- Bounded coding, review, and source-security-audit loops.
- Independent pull-request review with structured findings.
- Source-only security audits with pinned skills, fresh contexts, validators,
  and explicit `needs_validation` outcomes.

The largest remaining gap is completion integrity. Coding workers are told to
run relevant tests, but the model currently decides which checks are relevant.
A model-generated summary and command list are useful evidence, but they are
not a deterministic definition of done.

## Priority 1: Completion integrity

### Structured task contracts

Normalize every coding request into a bounded contract before dispatch:

- Goal.
- Constraints.
- Deliverable.
- Acceptance criteria.
- Actions requiring approval.
- Behavior when blocked.
- Optional allowed paths.

Slack requests may begin as natural language, while Notion requests may already
contain structured requirements. The runtime should preserve the original
request and store the normalized contract with the durable job pointer.

#### Contract acceptance criteria

- The contract is visible in Slack before or when the job is dispatched.
- Missing acceptance criteria are reported instead of silently invented.
- The worker receives the immutable contract.
- The pull-request body includes the contract and its final disposition.

### Deterministic completion gate

Add repository-configured validation commands that run independently after the
coding worker exits and before credentials are introduced for commit, push, or
pull-request creation.

The gate should use command arrays rather than arbitrary shell text. Commands
must pass the existing executable allowlist and run with a scrubbed environment
that excludes inference, GitHub, Slack, MCP, and application credentials.

The model may run exploratory tests while editing, but it cannot select,
remove, or weaken the trusted completion gate.

#### Gate acceptance criteria

- Target repositories define their required formatting, lint, type-check, unit,
  and end-to-end commands explicitly.
- The trusted wrapper, not the model, executes the final gate.
- A failing gate cannot produce a successful completion claim.
- No credentialed publication step runs until the gate has finished.
- Gate commands, exit codes, and bounded output are retained as an Actions
  artifact.

### Acceptance evidence

Generate a machine-readable evidence record that maps each acceptance criterion
to a deterministic check, observed artifact, or unresolved item.

The pull-request body should render the same record in a readable form. “Tests
passed” is insufficient when the task has several independent completion
conditions.

#### Evidence acceptance criteria

- Every criterion is marked `passed`, `failed`, `blocked`, or
  `needs_review`.
- Evidence links to a command result or named artifact.
- Missing evidence cannot be represented as success.
- The evidence record is generated or validated outside the model process.

### Repeated-failure circuit breaker

Keep the existing hard step and command limits, then add earlier detection for
unproductive loops.

Normalize each failed command into a fingerprint using the executable,
arguments, exit code, and bounded error signature. If the same failure occurs
twice without a relevant workspace change, stop that approach and require a
different plan or a blocked result.

#### Circuit-breaker acceptance criteria

- Repeated failures stop before the global command limit.
- Timeout, invalid arguments, missing information, test failure, permission
  denial, and infrastructure failure are classified separately.
- Slack receives a concise explanation of what was attempted and why execution
  stopped.

## Priority 2: Independent verification and recovery

### Contract-aware verifier

Run a fresh verifier after deterministic checks and before publication. The
verifier receives the immutable contract, diff, protected-path result, and
completion evidence.

Its job is to find reasons to reject the change:

- An acceptance criterion lacks evidence.
- The diff exceeds the stated scope.
- A test was deleted, skipped, or weakened.
- The implementation introduces an obvious correctness, security, reliability,
  or test-coverage defect.

The verifier recommends a result. Deterministic policy decides whether to block,
create a draft pull request, or create a review-ready pull request.

### Durable job checkpoints

Thread state currently retains the durable workflow pointer, but an interrupted
coding worker cannot resume its internal progress. Add bounded checkpoints at
phase boundaries:

- Contract accepted.
- Context gathered.
- Changes produced.
- Completion gate executed.
- Verification completed.
- Publication completed.

Checkpoint data belongs outside the target checkout and must not contain
credentials, raw prompts, full model completions, or unbounded command output.
A resumed run must verify the repository commit and checkpoint schema before
continuing.

### Token and cost budgets

Agent, step, command, and time limits do not provide a token ceiling. Aggregate
reported inference usage across parent and delegated contexts and support:

- Maximum input tokens.
- Maximum output tokens.
- Maximum estimated cost when operator-supplied model pricing is available.
- Warning thresholds before the hard limit.

Never infer vendor pricing. If the inference endpoint omits usage, report the
budget as unknown rather than estimating it as fact.

## Priority 3: Learning from failures

### Failure reports

Produce a compact failure artifact containing:

- Failure class.
- Phase and command fingerprint.
- Attempts made.
- Relevant bounded output.
- Workspace state and changed-file summary.
- Suggested next action.

Slack should receive the conclusion and workflow link, not raw logs or
potentially sensitive output.

### Harness-improvement backlog

Recurring failures should create proposed infrastructure improvements:

- A deterministic test.
- A lint or type rule.
- A protected path.
- A clearer tool error.
- A repository-map correction.
- A contract field or policy check.

Do not let the model automatically modify policy, workflow, or contributor-rule
files. A maintainer must review and apply each proposed harness change.

### Concise repository maps

Keep the root `AGENTS.md` focused on boundaries, hard rules, validation, and
links to detailed documentation. Add instructions only when they prevent an
observed failure that cannot be enforced more reliably in code.

Rules should move toward deterministic enforcement:

1. Document the rule.
2. Add it to a checklist.
3. Automate the check.
4. Enforce it in the workflow.

## Non-goals

- Automatically merging generated pull requests.
- Giving the model GitHub, Slack, MCP, or deployment credentials.
- Treating GitHub Actions or MCP as a sandbox for hostile code.
- Executing target code during source-only security audits.
- Persisting raw prompts, model completions, MCP arguments, or MCP results.
- Automatically rewriting project policy after a failure.
- Replacing human security review, risk acceptance, or production approval.

## Suggested delivery order

1. Define the task-contract and evidence schemas.
2. Add repository-configured deterministic completion gates.
3. Render acceptance evidence in pull-request bodies and Slack results.
4. Add repeated-failure detection and classified blocked outcomes.
5. Add the contract-aware verifier and deterministic publication policy.
6. Add checkpoint and resume support.
7. Aggregate token usage and enforce optional token and cost budgets.
8. Generate failure artifacts and a maintainer-reviewed improvement backlog.

## Open questions

- Should a failed completion gate block pull-request creation or create a draft
  pull request with explicit failed evidence?
- Where should each target repository declare its trusted validation commands?
- Which task-contract fields are mandatory for direct Slack requests?
- How should acceptance criteria be extracted from Notion without inventing
  missing requirements?
- What checkpoint granularity provides useful recovery without retaining
  sensitive conversation content?
- Which usage fields are consistently available across supported
  OpenAI-compatible inference endpoints?
- Should repeated-failure thresholds be global, per executable, or
  repository-configurable?
