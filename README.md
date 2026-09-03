# Multi-Agent Orchestration System

Four specialised agents collaborate to produce a market research report. Built on
[LangGraph](https://langchain-ai.github.io/langgraph/) with OpenAI models.

```
START ──▶ research ──▶ planning ──▶ writer ──▶ reviewer ──▶ END
                                      ▲            │
                                      └── revise ──┘
```

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
([graph.py](src/mas/graph.py#L31)) sends an unapproved draft back to the Writer
until `max_revisions` is hit, then publishes with the open issues listed. The
Reviewer's own `approved` flag drives that exit, so the node overrides an
approval that contradicts its own blocker issues rather than trusting the model
to be self-consistent.

**Revisions are surgical.** The Writer rewrites only sections with an issue
naming them (plus any report-wide issue naming none), so an approved section is
never destabilised by an unrelated fix — and a revision pass costs a fraction of
a full redraft.

**Dependencies are injected, not imported.** Models and the search tool arrive
through `Deps` ([deps.py](src/mas/deps.py)), so the test suite runs the entire
graph against a scripted fake with no network and no API key.

## Tests

```bash
pytest
```

22 tests covering the routing table, the revision loop, budget exhaustion,
citation validation, and the full graph end to end — all offline.

## Layout

```
src/mas/
  state.py        shared state + pydantic schemas
  graph.py        node wiring, routing, run_report()
  deps.py         dependency container
  config.py       env-backed settings
  llm.py          model factory (the only place OpenAI is constructed)
  cli.py          command line entry point
  agents/         research, planning, writer, reviewer
  tools/search.py DuckDuckGo backend + null backend
```

## Cost note

A default run is roughly 15–25 model calls: research (2) + planning (1) +
writer (one per section, per pass) + reviewer (one per pass). Drafting uses the
cheaper model and only review uses the stronger one; `--no-search` and
`--max-revisions 1` cut a run further.
