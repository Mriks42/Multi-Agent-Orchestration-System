# Multi-Agent Orchestration System

Four specialised agents collaborate to produce a market research report. Built on
[LangGraph](https://langchain-ai.github.io/langgraph/) with OpenAI models.

```
                            ┌──▶ write_section ──┐
                            │                    │
START ──▶ research ──▶ planning ──▶ write_section ──▶ assemble ──▶ reviewer ──▶ END
                            │                    │       ▲             │
                            └──▶ write_section ──┘       └── revise ───┘
```

Sections are independent LLM calls, so they fan out and run concurrently, then
merge through a reducer on `sections`. Measured on a 7-section report: **45s
sequential → 24s concurrent (1.9x)**. Not more, because research, planning and
review remain sequential — only the drafting phase parallelises.

With `--distributed`, that same fan-out dispatches to **separate worker
processes** through a shared queue instead of threads — see
[Running it distributed](#running-it-distributed).

| Agent | Responsibility |
| --- | --- |
| **Research Agent** | Plans search queries, runs them, distils hits into `Finding`s tied to their sources |
| **Planning Agent** | Turns findings into an outline: sections, purpose, key points, target length |
| **Writer Agent** | Drafts each section from the outline and findings; on revision, rewrites only what was flagged |
| **Reviewer Agent** | Fact-checks the draft against the findings, flags unsupported claims, approves or sends it back |

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows; use bin/activate elsewhere
pip install -e .
cp .env.example .env                                # then add your OPENAI_API_KEY

mas --company "Company X" --quarter "Q4 2025"
```

The report is written to `reports/company-x-q4-2025-<date>.md`.

```
Market research report: Company X — Q4 2025
draft model gpt-4o-mini | review model gpt-4o | up to 2 revision(s)

  OK Research Agent — 14 sources -> 11 findings
  OK Planning Agent — Executive Summary | Market Context | ...
  OK Writer Agent — pass 1, 6 section(s) drafted
  OK Reviewer Agent — changes requested (3 issue(s), 2 unsupported)
  OK Writer Agent — pass 2, 2 section(s) revised
  OK Reviewer Agent — approved (0 issue(s), 0 unsupported)
```

## Running it distributed

Section writing can run in separate worker processes instead of threads. No
server required — the queue is a SQLite file, so this works on one laptop or
across machines sharing a path.

```bash
# terminal 1 and 2 — as many workers as you like
mas-worker --queue mas-queue.db

# terminal 3
mas --company "Datadog" --quarter "Q4 2025" --distributed --queue mas-queue.db
```

Each worker logs what it claims, so you can watch a report get split up:

```
worker-A: claimed 'Financial Performance Analysis'
worker-B: claimed 'Executive Summary'
worker-A: claimed 'Growth Drivers'
worker-B: claimed 'Customer Metrics and Growth'
```

**To see the fault tolerance**, kill a worker (`Ctrl+C`, or `taskkill /F /IM
mas-worker.exe`) while it holds a section. Its lease stops being renewed, the
task returns to the queue, and another worker picks it up — the report still
completes. With no worker left running, the orchestrator waits out
`task_timeout` and exits with a clear error rather than hanging.

Redis is the natural backend for real deployment; `Broker` is a protocol, so it
is a drop-in alongside the SQLite one.

### Useful flags

| Flag | Effect |
| --- | --- |
| `--focus "pricing pressure"` | Extra angle every agent emphasises |
| `--max-revisions 3` | How many Reviewer → Writer loops before publishing anyway |
| `--no-search` | Skip web search; rely on model knowledge only |
| `--model` / `--reviewer-model` | Override either model |
| `-v` | Log every agent call |

## How it works

**One shared state, no hidden channels.** Every agent is a function
`ReportState -> partial update` ([state.py](src/mas/state.py)). LangGraph merges
the updates, so no agent mutates another's data. `trace` is annotated with
`operator.add`, which makes it append-only across the whole run.

**Evidence stays attached to claims.** The Research Agent emits `Finding`s
carrying `source_ids` into the source list, and the Research node drops any index
that points past the end — a hallucinated `[7]` never reaches the Writer as a
real footnote. The Reviewer then gets those findings as its *only* ground truth,
which is what makes fact-checking mean something concrete rather than a vibe check.

**The loop is bounded and the exit condition is enforced.** `route_after_review`
([graph.py](src/mas/graph.py#L50)) sends an unapproved draft back to the Writer
until `max_revisions` is hit, then publishes with the open issues listed. The
Reviewer's own `approved` flag drives that exit, so the node overrides an
approval that contradicts its own blocker issues rather than trusting the model
to be self-consistent.

**Revisions are surgical.** `plan_sections` dispatches only the sections with
an issue naming them (plus any report-wide issue naming none). An approved
section is never redrafted — it survives because the `sections` reducer merges
branches rather than replacing the map, so a section nobody dispatched simply
stays. A revision pass costs a fraction of a full redraft.

**Recollection is never laundered into fact.** This is the failure mode the
architecture invites: the Reviewer fact-checks the draft against the *findings*,
so if research invents a figure, the fact-checker will faithfully approve it. A
live `--no-search` run did exactly that — every number in the report was
fabricated and confidently stated. So a finding with no `source_ids` is rendered
to the model as `UNSOURCED`, the Writer must attribute its figures ("reportedly
$500 billion") instead of asserting them, the Reviewer treats a flatly stated
unsourced figure as a blocker, and every report carries a Provenance footer
counting what is actually backed by a source.

**Concurrency is a reducer, not a lock.** Parallel `write_section` branches
each return a single-key dict, and `merge_sections` combines them. No branch can
see or clobber another's work, so there is nothing to synchronise — the same
property that makes revisions surgical. Research queries run through a thread
pool, collected in query order so source indices, and therefore every citation,
stay stable across runs.

**Distribution is a lease, not a handover.** The defining problem of
distributed work is a worker dying while holding a task, so `claim()` leases
rather than hands over: a worker that stops renewing loses the task and it
returns to the queue ([broker.py](src/mas/distributed/broker.py)). On top of
that sit retries with attempt limits, idempotent completion so a late result
from a reclaimed worker cannot overwrite the real one, and lease renewal during
LLM calls so a slow call is not mistaken for a dead process. `SqliteBroker` gets
atomic claims from WAL mode plus `BEGIN IMMEDIATE` — without the immediate
transaction, two workers can read the same pending row and both claim it.

**One graph, two topologies.** With a broker, sections dispatch to worker
processes; without one, they fan out in-process. Both write an identical
`sections` update, so every downstream node is unchanged by the choice — and a
test asserts both paths produce byte-identical reports.

**Dependencies are injected, not imported.** Models and the search tool arrive
through `Deps` ([deps.py](src/mas/deps.py)), so the test suite runs the entire
graph against a scripted fake with no network and no API key.

## Tests

```bash
pytest
```

54 tests covering the routing table, the revision loop, budget exhaustion,
citation validation, provenance labelling, fan-out dispatch, broker leases and
retries, crash recovery, and the full graph end to end — all offline. One test
spawns two real subprocesses to prove the queue coordinates across processes.

## Layout

```
src/mas/
  state.py              shared state, pydantic schemas, section reducer
  graph.py              node wiring, routing, run_report()
  deps.py               dependency container (models, search, broker)
  config.py             env-backed settings
  llm.py                model factory — the only place OpenAI is constructed
  cli.py                `mas` entry point
  agents/
    research.py         plans queries, searches, extracts findings
    planning.py         findings -> outline
    writer.py           plan_sections / write_section / assemble
    reviewer.py         fact-checks the draft against the findings
    base.py             structured + free-text LLM calls, prompt rendering
  tools/
    search.py           DuckDuckGo backend + null backend
  distributed/
    broker.py           Broker protocol, Task lifecycle, lease semantics
    sqlite_broker.py    single-file queue: WAL + BEGIN IMMEDIATE claims
    worker.py           claim -> write -> complete loop, lease renewal
    cli.py              `mas-worker` entry point

tests/
  conftest.py           scripted fake model, Deps builder, writer-pass helper
  test_state.py         state construction, issue severity
  test_routing.py       the revise/publish decision table
  test_agents.py        per-agent behaviour with fakes
  test_provenance.py    unsourced findings never read as verified fact
  test_graph.py         full graph end to end, fan-out, revision loop
  test_broker.py        atomic claims, leases, retries, idempotency
  test_distributed.py   workers, crash recovery, two real subprocesses
  test_search.py        backend selection, graceful failure
```

Two commands are installed: **`mas`** runs a report, **`mas-worker`** runs a
worker that writes sections from the queue.

## Cost note

A default run is roughly 15–25 model calls: research (2) + planning (1) +
writer (one per section, per pass) + reviewer (one per pass). Concurrency cuts
wall-clock time, not cost — the same calls are made, just at once. Drafting uses
the cheaper model and only review uses the stronger one; `--no-search` and
`--max-revisions 1` cut a run further.
