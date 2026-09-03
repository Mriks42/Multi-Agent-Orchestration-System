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
pytest                          # 130 tests, all offline — no API key needed
```

Built on Python 3.14. Four commands: `mas`, `mas-worker`, `mas-eval`,
`mas-label`.

`.env` is gitignored and has never been committed — it will not come across with
the repo, so the key must be added by hand on each machine.

## Where things stand

19 commits, 130 tests passing, everything pushed. The pipeline works end to end
against the live API, and the distributed, resume and eval paths have all been
verified live rather than only in tests.

## Next steps, in priority order

Ask which of these to take up rather than starting one unprompted — they differ
a lot in cost and in how much of the user's own time they need. Item 1 in
particular cannot be done without them.

1. **Validate the LLM judge.** `mas-label` works and 2 pairs are built, but
   **none are labelled yet** — labelling is a human judgement the user has to
   make, by running `mas-label` and picking the better report in each pair. Ten
   labels is the threshold for a meaningful number, so this also needs ~5 more
   `mas-eval --smoke --judge` runs or 2 full-suite runs to generate enough
   pairs. Until it is done, the judge's scores should not be quoted.
2. **Cost and token tracking per agent.** Nothing measures spend. "The reviewer
   is 60% of cost" is the kind of concrete claim that gets asked about.
3. **A web API and minimal UI.** The project is CLI-only, so nobody who will not
   clone a repo can see it. `broker.submit` / `broker.stats` already have the
   right shape for a submit-and-poll API.
4. **Redis broker + Docker Compose.** `Broker` is a protocol, so this is one new
   file plus compose config. Blocked only on Docker not being installed.
5. **Write up the fabrication finding somewhere visible.** It lives in commit
   messages and the README's "How it works". It is the strongest interview story
   in the project and the least discoverable thing in the repo.

## Known limitations — deliberate, not oversights

Do not "fix" these without discussing; each was a considered trade-off.

- **The Reviewer checks the draft against the findings, not against reality.**
  If research collects something wrong, the fact-checker will faithfully approve
  it. Provenance labelling mitigates this; only an independent verification pass
  would solve it.
- **`sourced_finding_rate` measures whether *a* source exists, not whether it is
  any good.** A speculative blog post counts the same as an earnings release.
  Stripe scores 100% alongside NVIDIA because of this.
- **The judge's absolute scores compress at the ceiling** (everything gets 5/5).
  Pairwise deltas are the usable signal. Calibration anchors would be the fix.
- **No fiscal calendar.** `period.py` insists on a year and warns on companies
  with known offset fiscal years, but a real calendar needs per-company data the
  project does not have.
- **First-pass sections cannot see each other.** All sections are drafted
  concurrently, so they sometimes repeat. Siblings are passed on revision only.
  Drafting the Executive Summary in a second wave would fix it, at the cost of
  some parallelism.
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
- **Heredocs mangle `\n` inside Python string literals.** Several edits broke
  this way; use the Edit tool for anything containing escape sequences.

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
