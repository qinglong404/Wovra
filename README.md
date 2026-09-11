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
    safety.py       workspace root, audit hook, path guards, command-escape checks
    files.py        read/write/edit/delete/move/restore, search, checkpoints
    shell.py        run_command, process-tree kill
    background.py   background task registry and lifecycle
    web.py          web_search / web_fetch (with SSRF guard)
    interaction.py  ask_user, user hooks, current time
  cli/            Terminal entry point
    main.py         argparse, subcommand dispatch
    session.py      session lock, task/mode resolution
    prompt.py       system prompt assembly, Agent construction
    render.py       streaming turn rendering, replay
    interactive.py  chat loop, local commands
  blocks/         Zero-LLM block structure
    segment.py      round → blocks (per-file aggregation)
    labels.py       lifecycle labels → label line
    digest.py       block digests / inspection view
  task.py         persistent task tree (Task, TaskState)
  lifecycle.py    file lifecycle ledger
  llm.py          LLM client (single funnel for all model calls)
  tokens.py       token estimation
  ui.py           terminal rendering
  truncate.py     event indexing
```

---

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
> watermark organization and split analysis run in one pass, and a
> registry routes work inside a single runtime (one-way notify / two-way
> consult). Next: real long-task validation, and a dedicated human-view
> frontend (the terminal is a stopgap cockpit).

* [x] Minimal agent runtime (25 tools: files / commands / background tasks / web / interactive confirmation / plan ledger / history expansion)
* [x] Task representation and persistent task state (Task / TaskState / report.md / workspace-bound sessions)
* [x] Context management V3: zero truncation during execution, window guard, file map, anchor self-healing
* [x] Watermark-triggered batch organization → split (serial append pipeline, promoted on the next round; older views folded by current file state)
* [x] Safety (deny-list + sensitive-command confirmation + staleness guard + audit + atomic persistence)
* [x] Cost accounting (purpose-split / effective input / context occupancy / cache hits, persisted per round)
* [x] Two controlled comparison experiments (managed vs baseline, see results below)
* [x] Responsibility-based agent isolation, realized as context differentiation: the same organization pass yields responsibility domains, and a registry + one-way notify / two-way consult route work inside one runtime — a natural outgrowth of organization (single-agent branching, not synchronous agents)
* [ ] Pre-flight planning gate for open-ended / large-scope work (intent archived, not implemented)
* [ ] Heavy-load validation (synthetic long-trajectory replay + real long tasks)
* [ ] Dedicated human-view frontend (the terminal is a stopgap cockpit)
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
| [experiments/README.md](experiments/README.md) | Controlled experiment protocol and tooling |

The architecture will evolve through actual usage and experiments.

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

### Phase 5 — Human Collaboration

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
