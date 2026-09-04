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
pip install -e .
cp .env.example .env            # then add a real OPENAI_API_KEY
pytest                          # 211 tests, all offline — no API key needed
```

Built on Python 3.14. Six commands: `mas` (write a report), `mas-serve` (web
UI), `mas-worker` (section worker), `mas-eval` (score the pipeline), `mas-label`
(human labels for the judge), `mas-ablate` (vary the system, not the company).

`.env` is gitignored and has never been committed — it will not come across with
the repo, so the key must be added by hand on each machine.

## Where things stand

34 commits, 211 tests passing. The pipeline works end to end against the live
API, and the distributed, resume, eval and ablation paths have all been verified
live rather than only in tests.

## Next steps, in priority order

Ask which of these to take up rather than starting one unprompted — they differ
a lot in cost and in how much of the user's own time they need. Item 1 in
particular cannot be done without them.

1. **Cost and token tracking per agent.** Nothing measures spend, so nobody
   can answer "what does a run cost?" -- which came up repeatedly and could
   only be estimated. Capture usage per response and total it per agent.
2. **Write up the two findings somewhere a reader will see them** (see below).
3. **Validate the LLM judge — deferred, and unblocked.** 28 pairs are built and
   committed, so no further eval runs are needed. It waits only on the user
   spending ~20 minutes in `mas-label` picking the better report in each pair;
   ten labels is the threshold for a meaningful number. Lower priority than it
   looks: the ablation already validated the judge where it counts, by proving
   it catches fabrication. This adds a finer check on whether it shares a
   human's taste. Until it is done, do not quote an agreement figure.
4. **Deploy the UI somewhere.** `mas-serve` exists and works locally, but the
   project is still only visible to someone who clones it. A hosted link is
   what makes it shareable on an application. Note the job store is in-memory
   and single-process, which is fine for one instance and not for more.
5. **Redis broker + Docker Compose.** `Broker` is a protocol, so this is one new
   file plus compose config. Blocked only on Docker not being installed.

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
