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

mas --company "Shopify" --quarter "Q1 2025"
```

The report is written to `reports/shopify-q1-2025-<date>.md`. A real run:

```
Market research report: Shopify — Q1 2025
draft model gpt-4o-mini | review model gpt-4o | up to 2 revision(s)

  OK Research Agent — 23 sources -> 12 findings
  OK Planning Agent — Executive Summary | Financial Performance | Market ...
  OK Writer Agent — 5 of 7 section(s) drafted in parallel; 2 summarising section(s) follow
  OK Writer Agent — pass 1, 7 section(s) drafted
  OK Reviewer Agent — changes requested (2 issue(s), 2 unsupported)
  OK Writer Agent — pass 2, 2 of 7 section(s) revised
  OK Reviewer Agent — changes requested (2 issue(s), 2 unsupported)

Published with 2 unresolved issue(s) — revision budget was exhausted.
  ! [blocker] Financial Performance: the draft attributes $74.8bn GMV to the
    quarter, but the cited source reports it for the trailing twelve months.
```

**That ending is the normal one, and it is deliberate.** No report has yet been
approved: approval requires no blocking *or major* issues, so a single
substantive gap holds a report back, and the Reviewer keeps finding real ones.
Lowering that bar would raise the number and teach you nothing. The report ships
anyway, carrying a Review status footer that says it was published unapproved
and lists what is still open — a reader is told rather than left to assume.

**Use a real, publicly reporting company.** A fictional name gives the Research
Agent nothing to find, and the model will invent a company from scratch — the
Provenance footer then reports that nothing is sourced.

## Two things running it taught me

**The fact-checker was validating fabrications.** The Reviewer checks the draft
against the findings, so when research invented figures it faithfully approved
them -- and flagged the writer's honest "not disclosed" hedges as unsupported
instead. Fixed by propagating provenance through the state: unsourced findings
are marked, the writer must attribute rather than assert them, and every report
carries a footer counting what is actually backed by a source.

**The LLM judge preferred fabrication.** Tested against a `--no-search` control
it scored a report with zero citations 5/5 on "grounding" and 5 overall, above
the properly sourced baseline's 4. Not a limit of LLM judges -- a design error:
it was asked whether claims traced to the evidence while being shown no
evidence. Given the findings and sources, it scores that report 2/5 on
grounding, verified across three companies.

Neither was visible from reading the code. Both came from running it against a
case where the right answer was already known.

## The web UI

```bash
mas-serve            # http://127.0.0.1:8000
```

Type a company, watch the agents work, read the report. A report takes ~25
seconds, so submitting returns a job id and the page polls -- the same
submit-and-poll shape the distributed broker uses, one layer up. A spinner would
have hidden the pipeline, which is the part worth seeing:

```
Research Agent    9.9s  23 sources -> 10 findings
Planning Agent    5.7s  Executive Summary | Financial Performance | ...
Writer Agent      3.6s  4 of 7 section(s) drafted in parallel; 3 summarising follow
                        [4 in parallel: Market Dynamics, Operational Metrics, ...]
Writer Agent      3.2s  pass 1, 7 section(s) drafted
                        [3 in parallel: Recommendations, Outlook, Executive Summary]
Reviewer Agent    2.4s  changes requested (2 issue(s), 2 unsupported)
```

The branches that ran at once are named, and each agent's time is shown beside
it, because a flat list of steps reads as a sequence -- it hides the fan-out,
which is the one thing a page about orchestration should make obvious. The
numbers make the case on their own: four sections in 3.6s, where one section
alone takes about three.

The page also shows the provenance count and any unresolved reviewer issues
above the report, so a reader sees what is unverified before they read a figure.

Underneath sits the evidence itself: every finding with the sources backing it,
every source as a link, numbered to match the `[n]` markers in the text. The
count alone was a number to be taken on trust — "8 of 9 findings are backed by
a retrieved source" does not say *which* one is not, and a citation nobody can
follow proves nothing. Unsourced findings are marked in place, so the gap is
visible rather than merely counted.

## Deploying it

One image for every target — Render, Lightsail, EC2, Fargate and Hugging Face
Spaces all run a container and pass a port, so the platform stays a deploy-time
choice rather than something baked into the code:

```bash
docker build -t mas .
docker run -p 7860:7860 \
  -e OPENAI_API_KEY=sk-... \
  -e MAS_ACCESS=gallery -e MAS_ACCESS_CODE=your-code \
  mas
```

[render.yaml](render.yaml) deploys it to Render's free tier: point a Blueprint
at the repo and it asks for the two secrets rather than storing them anywhere.

**The free tier spins down after 15 minutes idle**, and a 30–60 second cold
start is fatal for a link someone clicks once — they see nothing and close the
tab. [A scheduled ping](.github/workflows/keep-warm.yml) keeps it awake. That is
not a workaround: the allowance is 750 instance-hours a month and no month is
longer than 744, so one service staying up fits inside what is already granted.
Set a `DEMO_URL` repository secret to the deployed URL to turn it on.

Hugging Face Spaces needs a PRO plan as of July 2026 — the Docker SDK is no
longer available on free accounts, and new free accounts cannot select CPU
Basic. The image runs there unchanged if you have one.

**Keep it to one instance.** Job state is in-process, so a second replica would
answer polls for jobs it has never heard of. No load balancer, no autoscaling,
no `--workers`. That is a real limit, not an oversight — `SqliteBroker` is the
upgrade path if it ever needs to outlive a process.

**Lambda and anything serverless is out.** A report takes 25–40s on a background
thread and the submit and the first poll are different requests; API Gateway
times out at 29s and nothing guarantees the poll reaches the same container.

### Who may spend the key

A public URL is an open invitation to spend your OpenAI credits at ~25 calls a
report. The answer is not to lock the door — a page that shows a stranger
nothing is a worse link than no link — so the default serves **real saved
reports** to everyone and gates only the part that costs money.

| `MAS_ACCESS` | anyone can | a code unlocks |
| --- | --- | --- |
| `gallery` *(default)* | read saved reports, with their evidence and open issues | running a new company |
| `locked` | nothing | everything |
| `open` | run anything | — |

`gallery` and `locked` refuse to start without `MAS_ACCESS_CODE`, rather than
falling back to open: a deployment that exposed the key because a variable was
misspelled is the exact failure the setting exists to prevent.

The saved reports in [gallery/](gallery/) are genuine runs kept whole —
timings, fan-out, evidence, and the reviewer's unresolved objections. A gallery
showing only the flattering parts would be a worse advertisement than none.

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

### The same queue, on Redis

`Broker` is a protocol and there are two implementations of it, which is the
only way that claim means anything. Pass a URL instead of a path and nothing
else changes — `open_broker()` picks the backend, so the orchestrator and the
workers cannot read one `--queue` differently:

```bash
mas-worker --queue redis://localhost:6379/0
mas --company "Datadog" --quarter "Q1 2025" --distributed --queue redis://localhost:6379/0
```

Where SQLite takes a write lock, Redis runs a Lua script: `EVAL` is atomic, so
the read-then-write inside a claim cannot interleave with another worker's.
Every conformance test in `tests/test_broker.py` runs against both backends
unchanged; the Redis ones skip when no server answers, so the suite stays green
offline. Install the client with `pip install -e ".[redis]"` — it is an optional
extra, because `SqliteBroker` is the default precisely so that nothing needs
installing.

`docker-compose.yml` runs the whole thing as separate containers — Redis, an
orchestrator and two workers:

```bash
docker compose up -d redis worker
docker compose run --rm orchestrator --company "Datadog" --quarter "Q1 2025" \
    --distributed --queue redis://redis:6379/0
```

That is the version SQLite cannot demonstrate: two containers with separate
filesystems, splitting one report over a queue that neither of them owns.
**It is still not faster** — broker round-trips cost more than the parallelism
saves. It buys fault tolerance and throughput across many reports.

## Surviving an interrupted run

Workers are recoverable, but the orchestrator holds the research, the outline
and every finished section. `--resume` persists those after each node:

```bash
mas --company "Cloudflare" --quarter "Q4 2025" --resume
# ...killed after research, planning and drafting...

mas --company "Cloudflare" --quarter "Q4 2025" --resume     # same command
# Resuming cloudflare-q4-2025-2026-09-02 — 8 findings; 7 sections planned;
#   7 written — next: reviewer
```

Measured: 22s of work before the kill, 8s to finish on resume — nothing
re-researched, nothing re-drafted. The thread id is derived from company,
quarter and date, so re-running the same command continues that report rather
than quietly starting a second one; `--fresh` forces a new run.

### Useful flags

| Flag | Effect |
| --- | --- |
| `--focus "pricing pressure"` | Extra angle every agent emphasises |
| `--max-revisions 3` | How many Reviewer → Writer loops before publishing anyway |
| `--no-search` | Skip web search; rely on model knowledge only |
| `--model` / `--reviewer-model` | Override either model |
| `-v` | Log every agent call |
| `--distributed --queue F` | Farm sections out to `mas-worker` processes |
| `--resume` | Persist progress; re-run the same command to continue |
| `--fresh` | With `--resume`, ignore saved state and start over |

`--quarter` is free-form but must name a year: `Q1 2025`, `FY2024`, `H1 2025`,
`2023-2025` all work; a bare `Q4` is rejected rather than letting the model pick
a year silently.

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
([graph.py](src/mas/graph.py#L68)) sends an unapproved draft back to the Writer
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

**A report that ships unapproved says so on its face.** The revision budget runs
out and the draft is published with the Reviewer's objections unresolved — which
is the honest behaviour, since more revisions do not converge. What was not
honest was the artifact: a polished document whose Provenance footer read "12 of
12 findings are backed by a retrieved source" while the fact-checker had raised
two blockers against it, visible only as a log line that scrolled past. The
verdict now travels with the report, listing every blocking and major issue left
open, alongside the caveat that review is against the findings and not against
reality. Approval is rare by design; a reader should be told, not left to assume.

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

**Task recovery and run recovery are different problems.** A lease returns a
dead worker's *section* to the queue, but the orchestrator holds the research,
the outline and every finished section — killing it used to lose all of that.
`--resume` checkpoints state after every node, so the same command continues
from where it stopped ([checkpoint.py](src/mas/checkpoint.py)). Thread ids are
derived from company, quarter and date rather than random, because a resume key
you cannot reproduce is not a resume key.

**Evals are deterministic first, judged second.** `metrics.py` scores provenance
and grounding as pure functions of the final state — no model, so a number moves
only when the reports do. Seeded-error probes then plant known defects and check
the Reviewer catches them; the first run scored 75%, missing a citation to a
source that did not exist, which is why `check_citations` now verifies indices in
code. `check_figures` extends the same idea to numbers
([figures.py](src/mas/agents/figures.py)): every currency amount, percentage and
magnitude in the draft is parsed and matched against the findings and sources,
and anything appearing in neither is raised as an issue. Point a reviewer that
approves everything at the probe suite and the code alone still catches three of
the four planted defects. The LLM judge covers what neither can reach — redundancy, purpose fit,
specificity — with its known biases controlled: pairwise comparisons run in both
orderings and a flipped verdict is scored as a tie, the rubric states outright
that length is not quality, and the judge is pinned at temperature 0. Its
absolute scores compress at the ceiling, so pairwise deltas are the signal.

**The period is validated, and periods are kept apart.** A live Shopify run
found four different "Q1 2025" revenue figures and wrote a whole section
attributing them to "variations in data interpretation" — when the likelier
explanation was that they described different periods. The system had no model
of time at all. It still has no fiscal calendar (that needs per-company data),
but it now insists the period names a year, tells the agents to treat a period
mismatch as the first explanation for conflicting figures, and warns on the
companies whose fiscal year is known to be offset
([period.py](src/mas/period.py)).

That warning used to go only to the model, which was half a fix. A live NVIDIA
run for "Q1 2025" pulled $26.0bn (fiscal Q1 2025, ending April 2024) and
$44.1bn (calendar Q1 2025) out of the sources and presented both as the same
quarter — both real, both cited, a year apart. The question was ambiguous and
the one party who could have resolved it was never told. The web UI now says so
before the run starts, and suggests the period written the way people write it
("Q1 FY2025" or "Q1 CY2025"). It is advisory: an ambiguous request still runs,
because sometimes ambiguous is what you meant.

**The judge is validated against a person, or its scores are not quoted.**
`mas-label` shows you two reports blind, records which you prefer, then replays
the judge over the same pairs and reports agreement. Below ten labels it refuses
to give a verdict at all, because a high rate over three items is noise. An
unvalidated judge produces numbers that feel rigorous and mean nothing, and this
is the step that usually gets skipped.

**A measure is only trusted once it has failed a test it could not fake.**
`mas-ablate` holds the company fixed and varies the system, with `--no-search`
as a known-bad control: it produces a wholly fabricated report, so any measure
scoring it the same as the real one is broken. That test found the LLM judge
rating a report with zero citations 5/5 on "grounding" and 5 overall, *above*
the properly sourced baseline. The cause was a design error -- the judge was
asked whether claims traced to the evidence while being shown no evidence. Given
the findings and sources it now scores that report 2/5, verified on three
companies. The same test found one of the deterministic metrics inverted too:
`unattributed_figure_count` measures hedging, and a fabricated report hedges
everything.

**Dependencies are injected, not imported.** Models and the search tool arrive
through `Deps` ([deps.py](src/mas/deps.py)), so the test suite runs the entire
graph against a scripted fake with no network and no API key.

## Tests

```bash
pip install -e ".[dev]"     # pytest and httpx; not needed just to run a report
pytest
```

298 tests covering the routing table, the revision loop, budget exhaustion,
citation validation, figure grounding, provenance labelling, fan-out dispatch,
that section drafting genuinely overlaps in time rather than only nominally,
broker conformance -- leases and retries, run against both backends,
crash recovery, checkpoint resume, eval metrics, the
judge's bias controls, the HTTP API, and that every command still imports and
parses — all offline. One test spawns two real subprocesses to prove the
queue coordinates across processes.

## Validating the judge

The LLM judge scores report quality, but nothing makes it right — so it is
checked against you before its numbers are used:

```bash
mas-eval --smoke --judge          # run twice, so there are two versions to compare
mas-label build                   # pair them up
mas-label                         # you pick the better one, blind
mas-label score                   # replay the judge, report agreement
```

You are never shown which report came from which run, and A/B order is shuffled
— a label that is not blind measures nothing. Below ten labels `mas-label score`
reports "insufficient labels to say" rather than a flattering percentage.

## Layout

```
src/mas/
  state.py              shared state, pydantic schemas, section reducer
  graph.py              node wiring, routing, run_report()
  deps.py               dependency container (models, search, broker)
  config.py             env-backed settings
  llm.py                model factory — the only place OpenAI is constructed
  cli.py                `mas` entry point
  checkpoint.py         durable run state, thread ids, resume
  period.py             reporting-period validation and fiscal hints
  agents/
    research.py         plans queries, searches, extracts findings
    planning.py         findings -> outline
    writer.py           plan_sections / write_section / assemble
    reviewer.py         fact-checks the draft against the findings
    figures.py          mechanical check: every figure must trace to the evidence
    base.py             structured + free-text LLM calls, prompt rendering
  tools/
    search.py           DuckDuckGo backend + null backend
  evals/
    metrics.py          deterministic scores from a finished state
    label.py            human label collection and judge agreement
    ablate.py           one company, several configurations
    seeded.py           planted-defect probes + clean control
    judge.py            LLM-as-judge with position/verbosity bias controls
    cases.py            the case set, spanning evidence coverage
    runner.py           suite execution, baselines, diffing
    cli.py              `mas-eval` entry point
    label_cli.py        `mas-label` entry point
    ablate_cli.py       `mas-ablate` entry point
  web/
    app.py              FastAPI submit-and-poll API
    access.py           who may spend the key: gallery / locked / open
    gallery.py          saved reports served to visitors without a code
    jobs.py             in-process job store, progress per agent
    index.html          the page
    cli.py              `mas-serve` entry point
  distributed/
    broker.py           Broker protocol, Task lifecycle, lease semantics
    sqlite_broker.py    single-file queue: WAL + BEGIN IMMEDIATE claims
    redis_broker.py     the same contract over Redis: atomic claims via Lua
    worker.py           claim -> write -> complete loop, lease renewal
    cli.py              `mas-worker` entry point

tests/
  conftest.py           scripted fake model, Deps builder, writer-pass helper
  test_state.py         state construction, issue severity
  test_routing.py       the revise/publish decision table
  test_agents.py        per-agent behaviour with fakes
  test_provenance.py    unsourced findings never read as verified fact
  test_access.py        access modes, the code check, misconfiguration
  test_figures.py       figure parsing, rounding tolerance, what must not flag
  test_review_footer.py published verdict, idempotent stamping, metric isolation
  test_cli_output.py    severity and model text survive rich markup
  test_graph.py         full graph end to end, fan-out, revision loop
  test_broker.py        Broker conformance suite: every backend, same guarantees
  test_distributed.py   workers, crash recovery, two real subprocesses
  test_search.py        backend selection, graceful failure
  test_checkpoint.py    resume skips completed work
  test_evals.py         metric determinism, aggregation, probes
  test_judge.py         position-bias control, judge validation
  test_period.py        period validation, fiscal hints, trace accuracy
  test_entrypoints.py   every module imports, every command parses
  test_web.py           the API end to end, offline
```

Six commands are installed: **`mas`** runs a report, **`mas-worker`** runs a
worker that writes sections from the queue, **`mas-eval`** scores the pipeline
against a stored baseline, **`mas-label`** collects human labels to validate the
judge, **`mas-ablate`** varies the system instead of the company, and **`mas-serve`**
runs the web UI.

## Cost note

A default run is roughly 15–25 model calls: research (2) + planning (1) +
writer (one per section, per pass) + reviewer (one per pass). Concurrency cuts
wall-clock time, not cost — the same calls are made, just at once. Drafting uses
the cheaper model and only review uses the stronger one; `--no-search` and
`--max-revisions 1` cut a run further.

`mas-eval` costs more: the full 12-case suite is ~12 reports plus 5 probe calls,
so start with `--probes-only` (5 calls) or `--smoke` (2 cases). `--judge` adds
one call per case.
