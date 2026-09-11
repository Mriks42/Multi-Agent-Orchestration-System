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
pytest                          # 217 tests, all offline — no API key needed
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

## Next steps, in priority order

Ask which of these to take up rather than starting one unprompted -- they differ
a lot in cost and in how much of the user's own time they need.

1. **Deploy the UI.** See "Deployment, as far as it got" below -- the options
   are worked out and two decisions are outstanding.
2. **A mechanical figure check.** The writer still invents figures -- a live
   Shopify run with 13 sourced findings and 22 sources produced "GMV increased
   by 38%", which no finding contained. `check_citations` is the proven pattern:
   deterministic code the model cannot talk itself out of, which took the
   citation probe from 75% to 100%. Extract numbers from the draft, compare
   against the findings, flag what appears nowhere. It will false-positive on
   derived figures ("up 31%" computed from two findings) and format mismatches
   ("$11.6 billion" vs "$11.6B"), so build the extraction with offline unit
   tests before wiring it into the reviewer. Expect it to reduce fabrication,
   not end it.
3. **Cost and token tracking per agent.** Nothing measures spend, so "what does
   a run cost?" can only be estimated -- it came up repeatedly.
4. **Write up the two findings** (see below) somewhere a reader meets them in
   the first thirty seconds rather than digging them out of the README.
5. **Validate the LLM judge -- deferred, and unblocked.** 28 pairs are built and
   committed, so no further eval runs are needed. It waits only on the user
   spending ~20 minutes in `mas-label`. Lower priority than it looks: the
   ablation already validated the judge where it counts.
6. **Redis broker + Docker Compose.** `Broker` is a protocol, so this is one new
   file plus compose config. Blocked only on Docker not being installed.

Not on this list, deliberately: **more replicates so the eval can resolve small
prompt changes.** It would cost 3x per experiment to detect effects that did not
matter; the resolution limit is documented instead.

## Deployment, as far as it got

Agreed this is the top priority: `mas-serve` works locally, so the project is
still only visible to someone willing to clone it, install it and supply an API
key. A hosted link is what converts the work into something a recruiter can
click.

**The constraint that rules out serverless.** A report takes 25-40 seconds and
runs on a background thread with in-memory job state, so Vercel, Netlify and
Lambda are all out -- request timeouts are shorter than a run, and nothing
persists between the submit and the first poll. It needs a long-running process.

**Options, with the trade-off that matters:**

| | cost | catch |
| --- | --- | --- |
| Hugging Face Spaces | free | no spin-down on CPU basic; AI-native audience |
| Railway | $5/mo credit | no spin-down while the credit lasts |
| Render free | free | spins down after 15 min idle, ~50s cold start |
| Fly.io | small free allowance | more configuration to get right |

Hugging Face Spaces was recommended. Render's free tier is the obvious default,
but a 50-second cold start is a real problem for a portfolio link: a visitor
clicks, sees nothing, and closes the tab before the app has started.

**Decision one, outstanding: which platform.**

**Decision two, outstanding: access control.** A public URL lets strangers spend
the user's OpenAI credits at roughly 25 calls a report. The options put to them,
undecided:

- an access code shared with recruiters, with a locked page otherwise
- a rate limit per IP only (open, but evadable and still costs money)
- a read-only gallery of pre-generated reports (zero risk, but nobody can try
  their own company)
- an access code plus a read-only fallback for visitors without it

**Division of labour.** Claude can write the Dockerfile, entry point,
environment handling and access control. Claude cannot deploy: that needs the
user's own account and their OpenAI key set as a secret on the platform, which
is about ten minutes of their clicking once the config exists.

## Known problems, stated plainly

- **The writer invents figures, and the system detects rather than prevents
  it.** This is the project's central unsolved problem. Item 2 above narrows it.
- **No report has ever been approved** -- 0 of 12 in the full suite, every run.
  Largely downstream of the above: the reviewer keeps finding invented figures
  and is right to. Worth knowing that approval also requires no *major* issues,
  so a single substantive gap blocks a report; that bar is a judgement call
  rather than a bug.
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
  project does not have.
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
  never for evaluating quality.
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
