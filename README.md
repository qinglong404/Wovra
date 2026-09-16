# Wovra

English | [简体中文](README.zh-CN.md)

> A runtime for structured, long-running AI work.

Wovra is an experimental system for organizing and managing complex, long-running work between humans and AI.

Instead of treating an AI agent as a single conversation that continuously accumulates context, Wovra treats a task as a persistent workspace with its own **state, context, progress, history, agents, reports, and evaluation**.

The goal is not to make AI smarter.

The goal is to make AI work **manageable, observable, steerable, and recoverable**.

---

## Why Wovra?

Current AI agents are becoming increasingly capable of completing complex tasks autonomously.

However, long-running tasks introduce a different set of problems:

* Context keeps growing as conversations and tool calls accumulate.
* Small changes may require carrying a large amount of irrelevant history.
* Failed attempts and intermediate results remain mixed with active context.
* Multiple agents can duplicate context and increase coordination cost.
* Humans often lose track of what the AI has done, what remains, and why it is blocked.
* Explanatory conversations and actual work conversations can interfere with each other.
* The AI that performs a task should not necessarily be the authority that decides whether the task is complete.

Wovra explores a different approach:

> **Separate the task itself from the conversations used to work on it.**

---

## Core Ideas

### 1. Persistent Work State

A task is not simply a conversation.

Wovra maintains a persistent representation of the work:

```text
Task
├── Goal
├── Requirements
├── Acceptance Criteria
├── Current State
├── Active Context
├── Report
├── History
├── Agents
└── Evaluation
```

The AI can continue working from the current state without carrying the entire history of every previous interaction.

---

### 2. Context Lifecycle

Not all information deserves to remain in the active context.

Wovra separates:

```text
Active Context
      │
      ├── Relevant information
      ├── Current decisions
      ├── Current state
      └── Immediate history
               │
               ▼
        Archived History
      ├── Previous attempts
      ├── Tool outputs
      ├── Debugging logs
      └── Detailed conversations
```

Information is not necessarily deleted.

Instead, unnecessary details can be folded into a compact representation and retrieved again when needed.

This allows the system to avoid repeatedly paying the cost of carrying irrelevant historical context.

---

### 3. Responsibility-Based Agents

Wovra does not assume that more agents means faster execution.

A single agent may be better for a simple task.

Multiple agents become useful when the task contains **different responsibilities or contexts**.

For example:

```text
                     Task
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Agent A     Agent B     Agent C
       Planning    Coding      Testing
          │           │           │
          └───────────┼───────────┘
                      ▼
                  Task State
```

Each agent can work with a focused context instead of inheriting the entire history of the main task.

The purpose of isolation is therefore primarily:

**responsibility separation + context isolation**

rather than simple parallelism.

---

### 4. Human-AI Alignment

Humans should not need to constantly supervise every tool call.

But they should be able to understand the state of the work at any time.

Wovra therefore uses a persistent report as a shared interface between humans and AI.

A report might contain:

```text
Current Status:
Implementing the authentication module.

Completed:
- API structure
- Database schema
- Login endpoint

Current Problem:
Token refresh occasionally fails after expiration.

Attempts:
- Reproduced the issue
- Checked middleware ordering
- Suspect refresh-token validation

Next Step:
Investigate token validation and expiration handling.
```

The report provides a compact answer to:

> What happened?
> Where are we now?
> What remains?
> What is blocking us?
> What should happen next?

---

### 5. Work and Explanation Are Different

Wovra distinguishes between **working on a task** and **understanding a task**.

A human may ask:

> "Why is this module implemented this way?"

or:

> "Explain how the current pipeline works."

Such conversations do not necessarily need to become part of the task's working context.

Only information that changes the actual work — such as a new requirement, decision, constraint, or discovered fact — should be promoted into persistent task state.

This keeps exploratory conversations from unnecessarily polluting the execution context.

---

### 6. Independent Evaluation

An agent should not be the only authority deciding whether its own work is complete.

Wovra separates:

```text
Execution
    │
    ▼
Agent produces result
    │
    ▼
Evaluation
    │
    ├── Accepted
    ├── Needs revision
    └── Failed
```

Acceptance criteria can therefore be evaluated independently from the agent's own claims.

This makes long-running autonomous work easier to verify and recover.

---

## Architecture

The conceptual architecture is:

```text
                    Human
                      │
             requirements / feedback
                      │
                      ▼
              ┌───────────────┐
              │     Wovra     │
              │               │
              │ Task Manager  │
              │ Context       │
              │ State         │
              │ Report        │
              │ Archive       │
              │ Evaluation    │
              └───────┬───────┘
                      │
              Agent Orchestration
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Agent A     Agent B     Agent C
          │           │           │
          └───────────┼───────────┘
                      │
                      ▼
             Existing Agent Runtime
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
        Files       Shell        Tools
```

Wovra is intended to focus on the **organization and lifecycle of AI work**, rather than reinventing every low-level capability.

Existing agent runtimes and mature tool implementations can be used underneath it.

### Code layout

The runtime is organized by responsibility; each module has a single job.

```text
src/wovra/
  agent/          Agent runtime (assembled from four mixins)
    core.py         run loop, round lifecycle, tool dispatch, usage accounting
    assembly.py     per-step context assembly, compact/collapsed views, expand
    maintenance.py  watermark-triggered organization → split, promote, baseline
    ledger.py       todo (milestone/step), notify/consult, submit guards
    prompts.py      model-visible prompts and tool schemas (pure data)
    support.py      run constants and stateless helpers (schema generation …)
  tools/          Built-in toolbox
    safety.py       workspace root, audit hook, path guards, command-escape checks,
                    confirm gate, write-protected zones, device read-only allow-list
    files.py        read/write/edit/delete/move/restore, search, checkpoints
    documents.py    native parsing of docx/xlsx/pptx/pdf/csv (stdlib only, no new deps)
    shell.py        run_command, process-tree kill
    background.py   background task registry and lifecycle (flags "exit 0 but looks like an error")
    web.py          web_search (Brave/Tavily/Serper/Exa API, local fallback) / web_fetch
                    (SSRF guard, readability extraction, cache)
    eyes.py         eyes: screenshot / view_image / page_text (image caps, "not injected" notice)
    interaction.py  ask_user, user hooks, current time
    limits.py       unified output caps + spill-to-disk for oversized results
    permissions.py  file permission guard (per-agent write/delete rights)
    status.py       the single authority for tool-result success/failure
  cli/            Terminal entry point
    main.py         argparse, subcommand dispatch
    session.py      session lock, task/mode resolution
    prompt.py       system prompt assembly, Agent construction
    render.py       streaming turn rendering, replay
    interactive.py  chat loop, local commands
  blocks/         Zero-LLM block structure
    common.py       shared base (event/message shapes)
    segment.py      round → blocks (per-file aggregation)
    labels.py       lifecycle labels → label line
    digest.py       block digests / inspection view
    migrate.py      load-time migration v1 coarse blocks → v3 by-file blocks
  task.py         persistent task tree (Task, TaskState)
  lifecycle.py    file lifecycle ledger
  llm.py          LLM client (single funnel for all model calls)
  tokens.py       token estimation
  ui.py           terminal rendering
  truncate.py     event indexing
  pathmatch.py    robust path matching (model-typed path forms normalized)
  registry.py     responsibility registry (domain tree → agent entries)
  routing.py      routing and the responsibility table
  views.py        per-domain view material (what each agent sees)
  economics.py    split economics (whether/where to split)
  split_lifecycle.py  split lifecycle (pending/ready/rejected/skipped/…)
  serve.py        web service (HTTP API + static frontend)
  __main__.py     `python -m wovra` entry
webui/            human-view frontend (static: index.html + vendor)
```

---

## Web UI

```bash
wovra serve                 # http://127.0.0.1:8600/ by default
wovra serve --port 8612     # another port (several instances can coexist)
WOVRA_TASKS_ROOT=/tmp/demo wovra serve   # serve a demo data dir (never touches real sessions)
```

The **conversation tab** lays a round out in time order: your message, the model's thinking and
answer (rendered as Markdown), every tool call with its result (collapsed by default — click for
the raw text), and one message block per agent involved.

![Conversation tab](docs/images/ui-conv.png)

The six KPIs on top are the ledger *at this moment*: **Σ prompt / cache hit rate / LLM calls /
rounds / tool calls / mean TTFT**. Two of them deserve a note: **tool calls** can exceed one per
step (a step may call several read-only tools in parallel — which is exactly how you see the
parallelism), and **mean TTFT counts working rounds only** — a maintenance call that reads 200K
tokens at once would otherwise drag the average up. The six cells refresh **per step**, not per
round. Each agent on the left has a context-occupancy bar (current/window plus observed peak).

At the bottom sits the **maintenance progress bar**: organization and split run on background
threads, and the job is finished the moment the round closes — without this bar those one or two
minutes look like a frozen page. It also reports the outcome, **including why a split was
rejected or skipped**.

**Open rounds** (unclosed) get a bar of their own: a round only closes when the model produces a
final answer, so an interrupted round (or one that ran out of steps) stays open and absorbs your
next message. The UI says so explicitly and offers **▶ resume** (equivalent to `/c`: continue
without injecting a new message):

![Open round and resume](docs/images/ui-open-round.png)

The **ledger tab** shows the current state and the state ledger produced by organization
(escalations / pending experiments / decisions):

![Ledger tab](docs/images/ui-ledger.png)

The **project tab** shows the file tree (ownership, state, description) next to each agent's
**responsibility** and the files it owns — the responsibility text comes from the split stage,
while file ownership is computed mechanically from paths:

![Project tab](docs/images/ui-project.png)

The **usage tab** breaks the bill down: rounds attributed by stage, aggregated per agent, cache
hits reconciled line by line:

![Usage tab](docs/images/ui-usage.png)

> They are real screenshots of a real session (the work happened in this very repository),
> served by `wovra serve`. The UI also ships a **workspace picker** (choose the working directory
> when creating a session, cross-platform) and an approve/auto safety toggle.

## CLI and local commands

| Command | What it does |
|---|---|
| `wovra run "<goal>"` | one-shot task (finishes organization/split before exiting) |
| `wovra chat [id]` | interactive session (input history, status bar, local commands) |
| `wovra list` · `wovra report <id>` · `wovra maint <id>` | list sessions · human-view report · maintenance ledger |
| `wovra views <id>` | print each agent's assembled view (debug "who sees what") |
| `wovra serve [--port N]` | start the Web UI |
| `wovra delete <id>` | delete a session (and its local data) |

Inside a session, **local commands** (`/` or `\` prefix, zero model cost): `/c`·`\c` **resume**
the most recent open round (no new message injected), `/report`, `/todo`, `/maint`, `/bg`, `/help`.
`Ctrl+C` exits at the prompt and interrupts the running round otherwise (the round stays open and
can be resumed). `--mode managed|baseline` switches the context strategy; `approve` (ask before
sensitive operations) / `auto` (let everything through, good for unattended runs) switches the
safety mode.

## Tooling and safety

* **Full output by default**: tool output is capped at 200,000 characters
  (`WOVRA_OUTPUT_LIMIT`); when it really overflows you get a head preview plus the original size
  and an on-disk path (`output/spill/`) that `read_file` can fetch at any time — **nothing is
  lost**.
* **`tasks/` is write-protected**: session data is the Runtime's source of truth, so the tool
  layer refuses writes and allows reads — and this rule **cannot be authorized** (out-of-workspace
  access can be authorized once; this cannot).
* **Web search through a professional API**: fill in any of `Wovra_Tavily`, `Wovra_Serper`,
  `Wovra_Exa`, `Wovra_Bocha`, `Wovra_SerpAPI`, `Wovra_Firecrawl` (the vendor-standard
  `*_API_KEY` names and the generic `Wovra_SEARCH_KEY` also work). Each search asks a randomly
  chosen backend first and moves to the next one if it fails, so the free quotas are spread
  across vendors rather than burnt on one; `Wovra_SEARCH_PROVIDER` pins one to the front. The
  vendor's ranking is the answer — no keyword-overlap filtering on top. With no key configured,
  or when every API fails, it falls back to a **single** local scraping channel (DuckDuckGo lite,
  10s budget) and **labels the result "local fallback, no relevance guarantee"**, because a bare
  "no results" reads to a model as "this doesn't exist online" — the wrong conclusion, measured on
  the GAIA run. (Firecrawl is also wired into `web_fetch`: when our own extraction comes back
  with almost nothing — JS-rendered pages — it asks Firecrawl for clean Markdown.)
* **Attachments are parsed natively**: `read_file` auto-detects `.csv`/`.tsv` (with GBK fallback
  and the encoding named in the header), `.docx`/`.xlsx`/`.pptx` (ZIP + XML) and PDF text layers —
  all stdlib, still zero new dependencies. Previously an agent had to install its own parsers: one
  audio task in the GAIA run left a 392MB venv plus a 1.9GB HuggingFace cache in its workspace.
  When a document cannot be parsed the reply says so and suggests the next step — it never hands
  back mojibake.
* **Image caps**: an image the model can "see" is at most **3000px** per side
  (`WOVRA_IMAGE_MAX_SIDE`, to save tokens), and the provider's hard limit is **8192px**
  (`WOVRA_IMAGE_HARD_MAX_SIDE`, measured: 8192 accepted, 8193 rejected). Oversized images are
  **not injected**, and the next reply tells the model plainly "you did not see this image"
  rather than letting it improvise. A per-turn view budget (`WOVRA_IMAGE_VIEWS`, default 6) nudges
  the model to conclude from the images it has seen, and at the hard ceiling
  (`WOVRA_IMAGE_VIEWS_MAX`, default 12) further `view_image` calls are **refused** — two visual
  GAIA tasks had burned their full 900s re-cropping the same picture.
* **Boundaries and confirmations**: commands stay inside the workspace; crossing out of it needs
  a one-time authorization recorded in `.wovra/authorized-paths.json`; destructive operations
  (`rm -r`, `git push/reset/…`) go through a **confirm gate** (y/N, with an "always this kind"
  option); a read-only device allow-list (`/dev/tty*`, `/sys/bus/usb/devices`, …) permits
  hardware-troubleshooting reads while still blocking write intent.
* **One authority for success/failure**: `tools/status.py` (first-line anchoring plus structured
  `exit_code`) is shared by the CLI, the UI, reports and the file ledger — no more guessing from
  the word "error" appearing in the text (that produced 42 false positives).
* **File permission guard**: `tools/permissions.py` limits write/edit/delete by agent ownership —
  "whether you may, not whether you should".
* **Audit**: every mutating tool call keeps a full audit trail; background tasks whose output
  looks like an error despite `exit_code=0` are flagged.

## Design Philosophy

Wovra follows a few simple principles:

### Low cost first

Do not spend tokens maintaining context that does not contribute to the current task.

### Isolation before parallelism

Multiple agents should exist because their responsibilities or contexts are meaningfully different, not simply because parallel execution looks impressive.

### Persistent state over conversation history

The current state of the work should be more important than the entire history of the conversation.

### Human-readable progress

A human should be able to understand the state of a long-running task without reading thousands of tool calls.

### Recoverability

Failures, previous attempts, and decisions should remain recoverable rather than disappearing when a context is compressed.

### Evaluation outside execution

The system performing the work should not be the sole judge of whether the work succeeded.

---

## Relationship to Existing Agent Tools

Wovra is not intended to replace existing coding agents or tool runtimes.

It can instead operate as an orchestration layer above them.

For example:

```text
                    Wovra
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   Agent Runtime  Agent Runtime  Agent Runtime
        │             │             │
        ▼             ▼             ▼
      Tools         Tools         Tools
```

This makes it possible to experiment with different underlying agents without changing the higher-level task organization model.

---

## Current Status

> **Mechanism baseline stage (V3).** The context management mechanism
> has been validated by controlled multi-session experiments and frozen
> (see [docs/context-management-v3.md](docs/context-management-v3.md)).
> Agent organization is now implemented as **context differentiation**:
> watermark organization and split analysis form a **serial two-stage
> append pipeline** (org → split; split is skipped if org fails), and a
> registry routes work inside a single runtime (one-way notify / two-way
> consult). The **human-view frontend has shipped** (`wovra serve` +
> `webui/`, see [Web UI](#web-ui)). Next: real long-task validation and
> independent task evaluation.

* [x] Minimal agent runtime (**32** tools in managed mode, **22** in baseline: files / commands / background tasks / web / **eyes (screenshot · view_image · page_text)** / interactive confirmation / plan ledger / history expansion / routing / notify-consult / join / responsibility / **organization & split submission**)
* [x] Task representation and persistent task state (Task / TaskState / report.md / workspace-bound sessions)
* [x] Context management V3: zero truncation during execution, window guard, file map, anchor self-healing
* [x] Watermark-triggered batch organization → split (**serial two-stage append pipeline**; the product takes effect **at the round-close boundary**, with "next round open" as the fallback for late async maintenance; older views folded by current file state)
* [x] Split product = **a structure tree + one responsibility per agent** (nodes declare scope with `path`/`paths`): file ownership (deepest path prefix), file-level descriptions (taken from the file head), non-LIVE file attachment, whether/where to split — all computed mechanically by the Runtime. An **untrustworthy product means no split** (truncated or incomplete coverage → keep the organization result and re-judge at the next watermark batch; "a wrong split is worse than no split")
* [x] Safety (**write-protected `tasks/`** (read-only, not even authorizable) + deny-list + sensitive-command confirmation (`rm -r` and destructive git go through the confirm gate) + device read-only allow-list + staleness guard + audit + atomic persistence)
* [x] Cost accounting (purpose-split / effective input / context occupancy / cache hits, persisted per round)
* [x] Two controlled comparison experiments (managed vs baseline, see results below)
* [x] Responsibility-based agent isolation, realized as context differentiation: the **split stage** produces the structure tree and the responsibilities, and a registry + one-way notify / two-way consult route work inside one runtime — a natural outgrowth of organization (single-agent branching, not synchronous agents)
* [x] Human-view frontend: `wovra serve` (six tabs + six top KPIs + maintenance progress bar + resume for open rounds + workspace picker)
* [ ] Pre-flight planning gate for open-ended / large-scope work (intent archived, not implemented)
* [ ] Heavy-load validation (synthetic long-trajectory replay + real long tasks)
* [ ] Independent task evaluation

## Experiment Results

Two controlled comparison sessions (same Gradio project, same frozen
starting file, same task book "implement 10 features round by round",
A = managed 14 rounds / B = baseline 15 rounds):

| | A managed | B baseline | B/A |
|---|---:|---:|---:|
| Nominal tokens | 2,255,448 | 4,966,437 | 2.20× |
| Effective input (cache-adjusted) | 377,668 | 475,686 | 1.26× |
| Including management overhead | ≈475,779 | ≈475,686 | ≈1.00× |
| Steps | 112 | 153 | 1.37× |

Three key conclusions (details in
[docs/context-management-v3.md](docs/context-management-v3.md)):

1. **Execution-time truncation is counter-productive**: three mechanism
   generations measured — in-round folding caused 39 re-reads, the 2KB
   keyhole sank 11 reads; with zero truncation the same workload
   finished in 3 reads;
2. **Cache hit rate is a discount, the base is the tax base**:
   baseline hit 99.3% and still paid dearly on a bloated base — the
   target of context management is the base, not the hit rate;
3. **Organizing every round is premature optimization**: 14 rounds of
   organization cost 98K tokens (~7K/round, exceeding some rounds' own
   work cost) — V3 defers to watermark-triggered batch organization.

Full data and derivation:
[docs/managed-vs-baseline-11rounds.md](docs/managed-vs-baseline-11rounds.md) ·
[docs/round11-context-experiment.md](docs/round11-context-experiment.md) ·
[docs/context-management-v3.md](docs/context-management-v3.md)

## Docs

| Doc | Content |
|---|---|
| [docs/context-management-v3.md](docs/context-management-v3.md) | Mechanism baseline V3 (experiment-corrected, current) |
| [docs/context-management-explained.md](docs/context-management-explained.md) | Mechanisms explained (plain-language) |
| [docs/context-runtime-v2.md](docs/context-runtime-v2.md) | V2 design spec (historical, with revisions) |
| [docs/managed-vs-baseline-11rounds.md](docs/managed-vs-baseline-11rounds.md) | Observational comparison data |
| [docs/round11-context-experiment.md](docs/round11-context-experiment.md) | Three-generation mechanism experiment |
| [docs/preflight-planning-intent.md](docs/preflight-planning-intent.md) | Pre-flight planning gate for open-ended / large-scope work (intent archive, not implemented) |
| [docs/venv-usability-prompt-reconcile-20260911.md](docs/venv-usability-prompt-reconcile-20260911.md) | Runner-experience fix: venv usability + system-prompt reconciliation (2026-09-11) |
| [docs/organization-split-explained.md](docs/organization-split-explained.md) | Organization → split explained (product shape, when it takes effect) |
| [docs/context-differentiation-runtime.md](docs/context-differentiation-runtime.md) | Context-differentiation runtime (responsibility domains, view assembly) |
| [docs/worklog-20260911.md](docs/worklog-20260911.md) | Engineering log: motivation / evidence / rollback per change (ongoing) |
| [docs/maint-progress-command-20260911.md](docs/maint-progress-command-20260911.md) | Maintenance progress accounting (cost and state of org/split) |
| [experiments/README.md](experiments/README.md) | Controlled experiment protocol and tooling |

The architecture will evolve through actual usage and experiments.

## Developer Notes

**Safety layer vs. the venv (2026-09-11).** `run_command` refuses commands
that traverse out of the workspace, including *in-root symlinks that point
outside it* (`_linked_outside`). An uv-created `.venv/bin/python` is exactly
such a link — it points at the system interpreter — so `.venv/bin/python
-m pytest` is *rejected by design*. Use `uv run` instead:

```bash
uv run python -m pytest        # tests
uv run python -m wovra --help  # CLI
```

When such a command is blocked, `run_command` now adds a hint pointing to
`uv run`. The system prompt also instructs the model to prefer `uv run`
over direct `.venv/bin/...` calls. If you genuinely need to reach something
outside the workspace, explain why and ask a human to do it — the sandbox
will not do it for you.

**`tasks/` is read-only (2026-09-15).** The workspace's `tasks/` directory is the
source of truth for session records (rounds, blocks, registry, reports) and is
written only by the Runtime. The tool layer refuses writes to it — file tools
(write/edit/replace/restore/delete/move) and any `run_command` / `run_background`
command that writes into it are rejected, and unlike out-of-workspace access this
**cannot be authorized**. Reads are unrestricted: `read_file` / `search_files` /
`glob_files` work as usual, so auditing session data needs no permission. Extra
read-only directories can be added via `WOVRA_READONLY_DIRS` (`os.pathsep`-separated).

The full file-map / security design lives in
[docs/context-management-v3.md](docs/context-management-v3.md) and
[agent-test/security-hardening-20260910.md](agent-test/security-hardening-20260910.md).

---

## Roadmap

### Phase 1 — Minimal Runtime

Build the smallest complete execution loop:

```text
User
 ↓
LLM
 ↓
Tool Call
 ↓
Tool Execution
 ↓
Result
 ↓
LLM
 ↓
...
```

The purpose is to establish a working foundation rather than optimize it.

### Phase 2 — Task State

Introduce persistent:

* goals
* requirements
* state
* reports
* history
* acceptance criteria

### Phase 3 — Context Lifecycle

Implement:

* context selection
* compression
* folding
* archival
* retrieval
* context isolation

### Phase 4 — Agent Orchestration

Introduce responsibility-based agents and controlled task delegation.

### Phase 5 — Human Collaboration (**mostly shipped**)

`wovra serve` + `webui/` already provide: progress viewing (six tabs, six KPIs, maintenance
progress bar), intervention (stop the round, resume an open round), approval (approve/auto
toggle, confirm gate) and recovery (persistent sessions, resume after a crash). **Task editing**
(change goal/constraints/plan from the UI) and **explanatory dialogue** are still ahead.
The original list:

Add:

* progress inspection
* intervention
* task modification
* approval
* recovery
* explanation sessions

### Phase 6 — Evaluation

Build mechanisms for independently determining whether a task satisfies its acceptance criteria.

---

## Long-Term Vision

Wovra explores a simple question:

> **If AI can work for hours or days, what should the system around the AI look like?**

Today's agent interfaces are often centered around conversations.

Wovra explores a model centered around **work**:

```text
Conversation
      ↓
      ↓
      ↓
     Task
      │
      ├── State
      ├── Context
      ├── Agents
      ├── Reports
      ├── History
      └── Evaluation
```

The long-term goal is to make complex AI work feel less like:

> "I asked an AI to do something."

and more like:

> **"I assigned a piece of work to an intelligent system, and I can understand, guide, inspect, and recover it at any point."**

---

## License

License to be determined.
