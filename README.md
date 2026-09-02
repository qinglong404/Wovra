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

> **Early experimental stage.**

The initial implementation focuses on validating the core concepts rather than building a production-ready platform.

Current priorities include:

* [x] Minimal agent runtime
* [ ] Task representation
* [ ] Persistent task state
* [ ] Context management
* [ ] Report generation
* [ ] Context folding and retrieval
* [ ] Responsibility-based agent isolation
* [ ] Human intervention
* [ ] Independent task evaluation
* [ ] Execution history and recovery

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
