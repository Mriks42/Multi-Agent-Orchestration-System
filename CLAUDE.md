# Working notes for Claude

Architecture and usage live in [README.md](README.md). This file covers what the
README does not: where the work stopped, what is deliberately unfinished, and
the traps worth knowing before changing anything.

## Purpose

A portfolio project for AI engineering job applications. That shapes priorities:
**the value is in what can be explained about it, not in the reports it
produces.** Depth on provenance, fault tolerance and evaluation beats new
features.

## Setting up a new machine

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; bin/activate elsewhere
pip install -e ".[dev]"         # the [dev] extra is what brings in pytest
cp .env.example .env            # then add a real OPENAI_API_KEY
pytest                          # 319 tests, all offline — no API key needed
```

Built on Python 3.14. Six commands: `mas` (write a report), `mas-serve` (web
UI), `mas-worker` (section worker), `mas-eval` (score the pipeline), `mas-label`
(human labels for the judge), `mas-ablate` (vary the system, not the company).

`.env` is gitignored and has never been committed — it will not come across with
the repo, so the key must be added by hand on each machine.

## Where things stand

The pipeline works end to end against the live API, and the distributed,
resume, eval and ablation paths have all been verified live rather than only in
tests. `pytest` reports the test count; `git log` reports the rest -- a number
written here is stale by the next commit, so this file records what is true
rather than what is countable.

**As of 2026-09-10.** The web UI is the part that moved most recently: it shows
the fan-out and per-agent timings, runs the revision loop (`max_revisions`
defaults to 2 there, so the revise cycle actually fires), warns before a run
when the period is ambiguous for an offset-fiscal company, and lists the
evidence -- every finding with its sources, unsourced ones marked, every source
as a link. Reports now carry a Review status footer stating whether they shipped
approved. Deployment config is written and pushed but nothing is live yet.

**As of 2026-09-23.** The project now also runs on a Mac (Apple silicon,
Python 3.14.7), set up from a clean clone by the recipe above with nothing
changed. Two things differ from the machine described below and both matter:
`render.com` **resolves here** -- `dashboard.render.com` answers 200 in 0.3s --
so the deploy is unblocked on this machine and needs no hotspot; and Docker
now exists here via colima, which unblocked step 6 and let the image finally be
built. A live Databricks run the same day put
the pipeline through end to end at 15 model calls, inside the 15-25 the README
claims.

Four things were found by *running* it rather than reading it, and all four are
worth knowing before changing that area:

- the Reviewer was fact-checking the generated Provenance footer and raising an
  objection no revision could ever satisfy (`report_body` in `agents/base.py`)
- `KNOWN_OFFSET_FISCAL` was missing three companies from the project's own eval
  suite, so every stored eval quietly asked 3 of 12 cases an ambiguous question
- the documented setup recipe could not work: pytest was never a declared
  dependency
- Hugging Face Spaces, the platform this file recommended, stopped being free

None of those were visible from the code. Prefer running the thing.

## Next steps, in priority order

Ask which of these to take up rather than starting one unprompted -- they differ
a lot in cost and in how much of the user's own time they need.

1. **Finish the deploy.** Everything Claude can do is done; see
   "Deployment: built, not yet live" below. It waits on ~10 minutes of the
   user's clicking in the Render dashboard. **On the Mac this is unblocked**
   -- `render.com` resolves and the dashboard answers -- so no hotspot is
   needed there; the DNS block recorded below is specific to the university
   network. Nothing here needs code.
2. **DONE 2026-09-24 -- the figure check fired on a real report.** It took
   n=3. The Datadog Q1 2025 run over the Redis/compose stack drafted a "13%"
   that appeared in no finding and no source, and `check_figures` raised it:
   *major*, section-less, exactly as designed. The two earlier runs (Shopify
   63 figures, Databricks 37) flagged nothing, so its live record is now one
   catch in three runs and still zero false positives.

   Read it for what it is. One catch is not a hit rate, and the check still
   cannot tell a derived figure from an invented one -- "13%" may well have
   been a legitimate derivation the writer failed to show, which is precisely
   why it raises major and offers the derivation route. What the run does
   settle is the thing n=1 could not: the check earns its place on a real
   report rather than only on the probe suite.

   Note what made it legible. The CLI had been swallowing severity labels
   (see Traps), so before that fix this would have printed as a bare
   `!  report:` and been indistinguishable from a Reviewer complaint. The bug
   fix is what let the mechanical catch be recognised as one.

3. **DONE 2026-09-24 -- cost and token tracking per agent.** `cost.py` holds a
   thread-safe `Ledger`; `ask`/`ask_text` take a `meter` from `Deps`, so agents
   still never import it. Structured calls needed `include_raw=True`: the
   parsed pydantic object carries no usage, and dropping the raw message under
   it is what made a run unmeasurable. Printed by `mas` and shown on the page.

   **The finding is where the money goes.** A live Confluent Q2 2025 run: 18
   calls, $0.04, and the **Reviewer spent ~75% of it on 2 of the 18 calls** --
   review is the one step on gpt-4o, at roughly 16x gpt-4o-mini per token.
   Counting calls, which is all this project could do before, pointed at the
   Writer and was exactly wrong. Worth telling people.

   Two honesty choices, both deliberate: an unpriced model reports tokens and
   *no* cost rather than $0.00 (a measure that moves the wrong way is worse
   than one that does not move), and a `--distributed` run says it counted only
   the orchestrator's calls, because workers keep their own ledgers. Prices
   were verified against OpenAI's published pricing rather than recalled -- the
   same discipline the hosting table and `period.py` needed.

4. **DONE 2026-09-24 -- the findings are the first thing a reader meets.**
   They were buried in commit messages and two thirds of the way down the
   README. "Four things running it taught me" now sits directly under the
   architecture diagram, before the agent table and the quick start.

   Four, not two: the original pair (the fact-checker validating fabrications,
   the judge preferring fabrication) plus the two this day produced -- that the
   mechanical figure check and the model reviewer fail in *different*
   directions, and that counting calls pointed at the wrong agent for cost.
   The through-line is stated once and is the actual pitch: none of the four
   was visible from reading the code.

5. **Validate the LLM judge -- deferred, and unblocked.** 28 pairs are built and
   committed, so no further eval runs are needed. It waits only on the user
   spending ~20 minutes in `mas-label`. Lower priority than it looks: the
   ablation already validated the judge where it counts.
6. **DONE 2026-09-24 -- Redis broker and Docker Compose both run.**
   `distributed/redis_broker.py` implements `Broker` over Redis: a Lua script
   per mutation, because `EVAL` is atomic and that is what buys the same
   property `BEGIN IMMEDIATE` buys in SQLite. `pending` is a ZSET scored by
   `created_at`, so a reclaimed task returns to its place in the order rather
   than the back of a queue; `running` is scored by lease expiry so
   `reclaim_expired` is one range query.

   **The protocol claim is now proven rather than asserted**: all twelve
   conformance tests pass against both backends, with no change to any test
   body -- registering the backend was one `BACKENDS` entry. `open_broker()`
   picks the backend from the queue string so `mas` and `mas-worker` cannot
   read one `--queue` differently.

   `docker-compose.yml` stands up Redis, two workers and an orchestrator.
   Verified live: a Datadog Q1 2025 report where two *containers* with
   separate filesystems split the drafting over one Redis queue. Still not a
   speedup, and the file says so.

   `redis` is an optional extra, not a runtime dependency -- `SqliteBroker`
   remains the default so a laptop run needs nothing installed. The image
   carries the client because it is also the worker image; the conformance
   suite skips the Redis backend when no server answers, so `pytest` stays
   green offline. Both were found by running it: the first compose boot died
   on `ModuleNotFoundError: redis` because the extra was not in the image.

Not on this list, deliberately: **more replicates so the eval can resolve small
prompt changes.** It would cost 3x per experiment to detect effects that did not
matter; the resolution limit is documented instead.

## Deployment: built, not yet live

Both decisions are made and the config is written, committed and pushed.
**Render free tier, Docker, access mode `gallery`.** What remains is the user's
ten minutes in a dashboard; there is no code left to write.

**What exists** (all on `main`, pushed to `github.com/Mriks42/Multi-Agent-Orchestration-System`):

- `Dockerfile` -- one image for every host. Runs as UID 1000 and defaults to
  port 7860 because Hugging Face wants both; reads `$PORT` so Render, Lightsail,
  EC2 and Fargate all work unchanged. **Built and run locally on 2026-09-24**
  (colima, linux/arm64): it built clean on the first attempt, served
  `/api/health` and the gallery, and `id` in the container reports uid 1000.
  The Render build is no longer a gamble -- though it builds amd64 there, and
  only the platform can prove that. One change since: the image installs the
  `redis` extra, because it is the worker image too.
- `render.yaml` -- Blueprint, `plan: free`, `numInstances: 1`, both secrets
  `sync: false` so the file describes the deploy without containing the key.
- `.github/workflows/keep-warm.yml` -- pings `/api/health` every 10 minutes.
  Needs a `DEMO_URL` repo secret; exits quietly without it.
- `web/access.py`, `web/gallery.py`, `gallery/*.json` -- four real saved reports.

**The three steps left for the user:** create the Blueprint at
dashboard.render.com, paste `OPENAI_API_KEY` and `MAS_ACCESS_CODE` when asked,
then set `DEMO_URL` as a GitHub secret to start the keep-warm.

**Their network blocks `render.com` at DNS.** Measured on 2026-09-10: the local
resolver returns NXDOMAIN for `render.com` while `github.com`, `huggingface.co`
and `google.com` all resolve, and `8.8.8.8`/`1.1.1.1` resolve it fine. It is a
narrow block on Render's own domain -- almost certainly the university network
(the machine is on ASU's AD). `onrender.com` is **not** blocked, so a deployed
app is reachable from there; only the dashboard is not. A phone hotspot for the
one-time setup is the fix. Do not suggest changing DNS on a managed machine.

**Why Render and not the others, as of September 2026.** The free-hosting
landscape collapsed and an earlier version of this file recommended a platform
that no longer works:

| | status |
| --- | --- |
| Hugging Face Spaces | **Docker SDK needs PRO** (~$9/mo) since July 2026; new free accounts cannot select CPU Basic at all |
| Fly.io | no free tier for new users since October 2024 |
| Koyeb | free tier closed to new signups after the Mistral acquisition, early 2026 |
| Render free | 750 instance-hours/month; spins down after 15 min idle, 30-60s cold start |
| Railway | ~$5/mo of credit, then paid |
| AWS Lightsail | $5/mo flat, and puts "deployed on AWS" on a CV |

Render's cold start is the only real objection, and the keep-warm answers it:
750 hours a month against a month of at most 744 means one service staying
awake fits *inside* the allowance rather than evading it. Say that out loud
rather than hiding it -- it looks like gaming a free tier and is not.

**Serverless is still out**, for the original reason: a report takes 25-40s on a
background thread with in-memory job state, so the submit and the first poll are
different requests and nothing guarantees they reach the same container. API
Gateway also times out at 29s. Lambda, Vercel and Netlify all fail on this.

**One instance, always.** No autoscaling, no load balancer, no `--workers`. A
second replica answers polls for jobs it has never heard of. `SqliteBroker` is
the upgrade path if it ever needs to outlive a process.

**A lesson worth keeping.** The platform advice in this file was stale and was
followed without checking, which nearly cost the user a wasted afternoon on a
dead end. Hosting free tiers change every few months. Verify pricing and
availability against the web before recommending a platform, the same way
`period.py`'s fiscal dates were checked against filings after recall got one
wrong.

## Known problems, stated plainly

- **The writer invents figures, and the system detects rather than prevents
  it.** Still the project's central unsolved problem, but narrower than it was.
  `check_figures` (`agents/figures.py`) parses every material figure -- currency,
  percentage, magnitude -- out of the draft and matches it against the findings
  and sources, raising an issue for anything grounded in neither. Against the
  probe suite with a reviewer that approves everything, the mechanical checks
  alone now catch 3 of 4 planted defects, up from 1. **It has now caught one
  live too**: a "13%" grounded in nothing, on the Datadog run of 2026-09-24 --
  the third live run to exercise it. What it does **not** do:
  - It cannot tell a derived figure ("up 31%", computed from two findings) from
    an invented one, which is why it raises "major" and not "blocker", and why
    the fix text offers the writer the derivation route explicitly.
  - It says nothing about attribution. A figure can be grounded and still stated
    too flatly; that is the UNSOURCED rule's job, and the one probe the
    mechanical checks still miss (`unsourced_stated_as_fact`) is exactly that
    case -- 4,000 is in the findings, only the wording is wrong.
  - Only *material* figures are checked. Bare counts, years and quarter labels
    are skipped deliberately: a false positive costs the writer a revision it
    needed for a real defect.
  - **It has no opinion about consistency.** Each figure is matched against the
    evidence on its own, so two figures that are both grounded and mutually
    contradictory both pass. The live Databricks run of 2026-09-23 is the
    worked example: the draft carried a $5.4bn Q4 2025 revenue run-rate and a
    $4.8bn December 2025 run-rate, both traceable to the evidence, and
    `check_figures` raised nothing while the Reviewer raised both as blockers.
    The two checks catch **disjoint** classes -- mechanical grounding and
    model-judged coherence -- which is the argument for keeping both, and a
    better one than "the mechanical check caught a fabrication" would have
    been. Worth telling people.
  - **It compares the draft against the findings, not against reality** -- the
    same limitation already recorded below for the Reviewer. A wrong figure
    that research collected is a figure the check will happily approve. This
    also rules out `--no-search` as an adversarial test of it: the writer takes
    its figures from the invented findings, so they match and pass.
- **No report has ever been approved** -- 0 of 12 in the full suite, every run,
  and none of the live runs since has been approved either -- eight of them as
  of 2026-09-24, across the CLI, the web UI and the compose stack. Largely downstream of
  the above: the reviewer keeps finding invented figures and is right to. Worth
  knowing that approval also requires no *major* issues, so a single substantive
  gap blocks a report; that bar is a judgement call rather than a bug.
  Do not chase this number -- the way to move it is to lower the bar. What was
  worth fixing was the *disclosure*: the published report now carries a Review
  status block stating that it shipped unapproved and listing what is still
  open (`stamp_review` in `agents/writer.py`). Note that `check_figures` raises
  `major`, so it can only push approval further away; that is the intended
  trade, and the reason approval rate is not a goal.
- **Severity assignment is inconsistent.** Redundancy has been seen labelled
  "blocker" when the prompt reserves that for factual errors. A real run,
  though, produced only genuine fabrications as blockers -- so this is
  occasional, not systematic. Do not generalise from blocker counts without
  reading the issue text, which the eval does not currently store.

## Three prompt experiments that failed

The reviewer repeatedly spends its whole revision budget on claims no finding
can support: the writer rewords them, the reviewer objects again. Three attempts
to fix it, measured over the same four companies at `--max-revisions 3`:

| variant | open issues | unattributed | citations |
| --- | --- | --- | --- |
| original | 17 | 1.75 | 32.2 |
| "delete unsupported material" | 7 | 4.25 | 27.0 |
| the same, plus a hedging carve-out | 9 | 3.50 | 21.8 |
| reviewer marks each issue delete/revise | 14 | 4.75 | 25.0 |

Every variant traded fabrication-resistance for fewer complaints: fewer
citations and more figures asserted without attribution. All three were
reverted. The original prompt scores best on the metrics this project actually
cares about, and open issues counts reviewer complaints rather than report
quality.

The honest reading is that the noise exceeds the effects -- see the resolution
note under Traps. Do not retry this without more replicates per condition.

## The two findings worth telling people

Both came out of running the thing rather than reading about it, and both are
currently buried in commit messages.

**The fact-checker validated fabrications.** The Reviewer checks the draft
against the findings, so when research invented figures, the fact-checker
faithfully approved them -- and flagged the writer's honest "not disclosed"
hedges as unsupported instead. Fixed by propagating provenance: unsourced
findings are marked, the writer must attribute them, and every report carries a
footer counting what is actually backed by a source.

**The LLM judge preferred fabrication.** Tested against a `--no-search` control,
it scored a report with zero citations 5/5 on "grounding" and 5 overall, above
the properly sourced baseline's 4. The cause was a design error: the judge was
asked whether claims were traceable to evidence while being shown no evidence.
Given the findings and sources, it scores the fabricated report 2/5 on
grounding -- verified on three companies.

## Known limitations — deliberate, not oversights

Do not "fix" these without discussing; each was a considered trade-off.

- **The Reviewer checks the draft against the findings, not against reality.**
  If research collects something wrong, the fact-checker will faithfully approve
  it. Provenance labelling mitigates this; only an independent verification pass
  would solve it.
- **The judge's absolute scores barely vary across reports from this pipeline.**
  Calibration anchors were tried and moved every score in lockstep (4 -> 3)
  without creating spread. The likeliest reason is that twelve reports from one
  pipeline genuinely are alike; the judge discriminates fine when there is a
  real difference. Use it for large quality gaps, not fine ones.
- **`unattributed_figure_count` measures hedging, not truth.** On Shopify the
  fabricated report scored *better* than the real one, because with nothing
  sourced the writer hedges everything. Fine as a within-pipeline signal,
  unreliable as a fabrication detector.
- **`sourced_finding_rate` is a tripwire, not a ruler.** It measures whether *a*
  source exists, not whether it is any good -- a speculative blog post counts the
  same as an earnings release, which is why private companies score 100%
  alongside NVIDIA. So it cannot rank normal reports, but it goes cleanly to 0.0
  on a fabricated one. Useful as an alarm, never as a quality score.
- **No fiscal calendar.** `period.py` insists on a year and warns on companies
  with known offset fiscal years, but a real calendar needs per-company data the
  project does not have. `KNOWN_OFFSET_FISCAL` is a curated list of eleven
  names, not a database -- any company outside it gets no warning, and there are
  hundreds. Treat a missing warning as "not checked", never as "calendar year".
  MongoDB was added on 2026-09-24 after a live run asked it for "Q1 2025" and
  got no warning: its Q1 FY2025 is the quarter ended 30 April 2024, so the
  calendar reading is a year out. Verified against the filings. That is the
  gap working as designed rather than a bug -- the list only knows what it has
  been told -- but it is worth adding a name each time one is found.
  The list was wrong about the project's own eval suite until 2026-09-10:
  Snowflake, Zscaler and Braze all run offset years and none were flagged, so
  **every stored eval before that date asked three of its twelve cases an
  ambiguous question**. Do not read a period-related result from those runs
  without allowing for it. A test now pins the eval companies and the web page's
  suggested examples against the list so the two cannot drift apart again.
- **Drafting is two waves, and the second one costs a round-trip.** Body
  sections go first in parallel; summarising sections follow with the bodies as
  siblings, so the executive summary can refer to the report instead of
  repeating it. Whether it reduced repetition is unmeasured -- the judge scored
  `non_redundancy` identically before and after, and that criterion does not
  vary, so the change is kept on reasoning rather than evidence.
- **The web job store is in-memory and single-process.** Jobs vanish on
  restart and do not span workers. Deliberate for a one-instance demo;
  `SqliteBroker` is the upgrade path if it ever needs to outlive a process.
- **Distribution is not faster** for a single report (30s vs 24s threaded).
  Broker round-trips cost more than they save. It buys fault tolerance and
  throughput across many reports — claim those, not a speedup.

## Traps

- **Never run with a fictional company** (`"Company X"`). Search finds nothing,
  the model invents everything, and the report looks plausible and is entirely
  fabricated. Use real, publicly reporting companies.
- **The README drifts.** Twice it fell out of date without being noticed. Audit
  it against the filesystem after any structural change:
  ```bash
  find src tests -name "*.py" ! -name "__init__.py" -printf "%f\n" | sort -u \
    | while read f; do grep -q "  $f" README.md || echo "NOT LISTED: $f"; done
  ```
  Sample CLI output in the README rots silently — re-read the examples whenever
  output format changes, since nothing checks those automatically.
- **`--no-search` produces fully fabricated reports.** Fine for wiring tests,
  never for evaluating quality. It is also **not** an adversarial test of
  `check_figures`: the writer draws its figures from the invented findings, so
  they match the evidence and pass.
- **A re-read from disk is useless if the browser does not ask.** `index.html`
  is re-read per request, but the response carried no cache headers, so the
  browser served its own copy and a corrected page kept rendering the previous
  JavaScript until a hard refresh. That looks exactly like a fix that did not
  work, and it wasted a round of "is it fixed yet?" on 2026-09-24. The route
  now sends `Cache-Control: no-store` and a test pins it.
- **`mas-serve` does not reload Python.** `index.html` is re-read from disk on
  every request, so HTML and CSS changes appear on a browser refresh -- but
  every `.py` module was imported at startup. After changing agent, web or
  access code, restart the process or the user is testing the old build while
  reading the new page. Cost real confusion once. `--reload` while iterating.
- **A pydantic body model must live at module level in `web/app.py`.** That file
  has `from __future__ import annotations`, so FastAPI resolves body types by
  name against module globals. A model defined inside `create_app` is invisible
  there and the route silently degrades to treating the body as a query
  parameter -- every request answers 422, with no error at startup to explain it.
- **Verify hosting and pricing facts against the web before recommending one.**
  Free tiers change every few months and this file has already been wrong once:
  it recommended Hugging Face Spaces, which stopped offering Docker on free
  accounts in July 2026. The same applies to any fiscal or pricing fact -- recall
  produced a wrong fiscal year for Snowflake here, caught only by checking
  filings.
- **More revisions do not produce better reports.** At `--max-revisions 3` the
  same four companies finished with *more* open issues than at 1 (17 vs 13),
  took 50% longer, and were approved exactly as often: never. All four used
  every revision without converging. Each rewrite is a fresh chance to invent
  something new, so the loop treadmills rather than converging. Keep the
  default at 1-2; raising it costs calls and buys nothing measurable.
- **The eval suite cannot resolve small prompt changes.** Four companies, one
  run each, is enough to detect a fabricated report against a researched one --
  the gap there is enormous -- and nowhere near enough for a prompt tweak.
  Datadog's `unattributed_figure_count` swung 2 / 10 / 5 / 11 across four runs
  of near-identical code. Any effect smaller than that is noise. Before
  believing a prompt result, replicate it; `evals/revise-*` holds the four runs
  where this was learned the expensive way.
- **A measure that moves the wrong way is worse than one that does not move.**
  `mas-ablate` reports correct/blind/inverted for exactly this reason. An
  earlier version called any difference "separates", which scored an inversion
  as a success.
- **Check how many cases a stored eval covers before quoting its aggregates.**
  `--smoke` runs only NVIDIA and Stripe, so their summary figures are two
  reports, and their high/low coverage breakdown is one company per tier. Read
  `len(result["cases"])` rather than assuming a run is suite-wide.
- **`rich` eats square brackets, including the model's.** `console.print`
  parses `[...]` as markup, so a severity label or a model-written citation
  marker like `[2]` vanishes silently unless escaped. This shipped and lasted:
  every CLI run printed its issues with **no severity at all**, and the README
  documented sample output the code could not produce. It went unseen because
  the markdown report footer renders severity correctly, and that is where it
  was being read -- the terminal and the file disagreed and only the file was
  checked. Fixed on 2026-09-23 with `rich.markup.escape`; `tests/test_cli_output.py`
  asserts on rendered text, because the f-string looked right the whole time it
  was wrong. Escape anything the model wrote before printing it.
- **An optional extra is not optional inside the image.** The first compose
  boot died on `ModuleNotFoundError: redis`: the client is an extra in
  `pyproject.toml` but the `Dockerfile` installs `requirements.txt`, which
  deliberately has no server dependencies. The image now installs it
  explicitly, because the image is the worker image too. Anything reached only
  through an extra needs checking against the image, not just the venv.
- **Heredocs mangle `\n` inside Python string literals.** Several edits broke
  this way; use the Edit tool for anything containing escape sequences.
- **Label with `mas-label`, not by editing `evals/labels.json`.** Hand-editing
  works and the tool reads it, but opening the file puts both drafts in front of
  you at once, which makes the judgement less independent. Rebuilding pairs also
  reassigns ids, so labels written against an older id format are dropped.

## Conventions

- Tests run fully offline against a scripted fake model (`tests/conftest.py`);
  no test may require an API key or network.
- Models, search and the broker are injected through `Deps` — never imported
  directly inside an agent.
- Every claim in the README that can be checked mechanically should be, and has
  been: test counts, file lists, line references.
- Live verification matters. Numbers quoted in commits and the README (45s→24s,
  22s→8s resume, 100% probe catch rate) come from real runs, not estimates. Keep
  it that way, and say "n=1" where it is one run.
