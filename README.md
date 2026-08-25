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

Manual: **Actions → Concurrency test → Run workflow**, with two inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `job_count` | `60` | Matrix jobs to fan out (1–256, GitHub's per-run matrix cap) |
| `sleep_seconds` | `60` | How long each job holds its runner |

Pick `sleep_seconds` comfortably longer than runner startup (~15–30s), or jobs
finish before their neighbours have booted and the overlap looks artificially
low.

## Reading the results

The `report` job writes a summary to the run page:

- **Peak observed concurrency** — the headline number. If it plateaus at 60
  while 120 jobs were requested, the org is still on the default standard-runner
  limit.
- **Total wall clock** — with a hard cap of C and a hold time of T seconds,
  N jobs take roughly `ceil(N / C) * T` plus startup overhead.
- **Timeline** — an ASCII chart of every job's window, which makes queueing
  visible as a staircase instead of one solid block.

### Suggested check for the limit increase

Run with `job_count: 120`, `sleep_seconds: 90`.

- Peak ≈ 120, one wave, wall clock ≈ 2 min → the 120-job limit is live.
- Peak ≈ 60, two staircase waves, wall clock ≈ 4 min → still capped at 60.

Repeat the same run before and after the change and the two summaries are a
clean before/after for the support ticket.

## Notes on what this does and does not measure

- Concurrency limits apply per account across all repositories, so an unrelated
  workflow running at the same time will lower the peak here. Run this on an
  otherwise idle org.
- `strategy.max-parallel` is deliberately not set, so the only ceiling is the
  account limit itself.
- The windows are measured inside the job, so runner provisioning time is
  excluded — this measures concurrently *executing* jobs, which is what the
  limit governs.
