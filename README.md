# test_actions_concurrency

Scratch repository for GitHub Support ticket work on the `global-snow-processing`
organization. Its only purpose is to put a real Actions workflow run on the
org's record so the standard-runner concurrency limit can be raised, and to
measure how many jobs actually run at once.

Safe to delete once the limit increase lands.

## What the workflow does

`.github/workflows/concurrency-test.yml` fans out N identical matrix jobs on
`ubuntu-latest`. Each job does nothing but sleep for a fixed number of seconds
and record the wall-clock window during which it held a runner. A final
`report` job collects those windows and computes the **peak observed
concurrency** with a sweep over the start/end events — that number is the
actual parallelism the account delivered, not just the number of jobs
requested.

Nothing is built, nothing is published, and no secrets are used.

## Running it

Automatic: any push runs a small 4-job / 30-second version, just enough to
prove Actions works in the org.

Manual: **Actions → Concurrency test → Run workflow**, with three inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `job_count` | `256` | Matrix jobs to fan out (1–256, GitHub's per-run matrix cap) |
| `hold_seconds` | `90` | How long each job holds its runner |
| `census_minutes` | `10` | How long to keep holding before draining the queue |

Keep `hold_seconds` comfortably longer than runner startup, which is ~15–30s and
is staggered across a large fan-out. At 30s holds the early jobs finish before
the later ones have booted, and the measured overlap comes out lower than the
real limit. 90s is a good floor; 120s is safer for fan-outs above 200.

`census_minutes` bounds the experiment. Once the window closes, jobs still
waiting for a runner exit immediately instead of holding one, so a low limit
gives a fast answer rather than an hours-long run.

### Going past 256 jobs

256 matrix jobs per workflow run is a hard GitHub limit, so a single run cannot
prove a limit above 256. To probe higher, dispatch several runs at the same time
(`Run workflow` a few times in a row, or the same via the API). Each run's
report aggregates **runner-level concurrency across every overlapping run in the
repository**, so four simultaneous 256-job runs measure the account-wide ceiling
up to 1024.

## Reading the results

The `report` job writes two sections to the run page.

**In-job concurrency (this run)** — measured from timestamps taken inside each
job, so runner setup is excluded. This is the count of jobs actually executing
at once. It also prints a timeline, where queueing shows up as a staircase
rather than one solid block.

**Runner-level concurrency (all overlapping runs)** — measured from the Actions
API using each job's `started_at`/`completed_at`, across every run in the repo
that overlaps this one. This is the number of runners the account had allocated,
and it is the number to compare against the concurrency limit. When several runs
are in flight it also breaks the peak down per run: if the overall peak is well
below the sum of the per-run peaks, the runs were competing for the same pool —
that is the account limit showing itself.

### Suggested check for the limit increase

Dispatch two runs back to back with `job_count: 256`, `hold_seconds: 120`.

- Peak occupied runners ≈ 60 → still on the Team default.
- Peak ≈ 120 → the requested increase is live.
- Peak plateauing well below 60 → something else is capping the org, worth
  raising with Support on its own.

Run the same thing before and after the change; the two summaries are a clean
before/after for the ticket.

## Notes on what this does and does not measure

- Concurrency limits apply per account across all repositories, so an unrelated
  workflow running at the same time will lower the peak here. Run this on an
  otherwise idle org.
- `strategy.max-parallel` is deliberately not set, so the only ceiling is the
  account limit itself.
- The windows are measured inside the job, so runner provisioning time is
  excluded — this measures concurrently *executing* jobs, which is what the
  limit governs.
