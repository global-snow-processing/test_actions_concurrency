# test_actions_concurrency

Scratch repository for GitHub Support ticket work on the `global-snow-processing`
organization. Its only purpose is to put a real Actions workflow run on the
org's record so the standard-runner concurrency limit can be raised, and to
measure how many jobs actually run at once.

Safe to delete once the limit increase lands.

## What the workflow does

`.github/workflows/concurrency-test.yml` fans out N matrix jobs on
`ubuntu-latest`, one per geographic tile. Each job runs the project's snowmelt
runoff onset detection over its tile (see [What each job
computes](#what-each-job-computes)) and records the wall-clock window during
which it held a runner. A final `report` job collects those windows and
computes, with a sweep over the start/end events, both the **peak occupied
runners** and the **queue depth at the same instants**.

Both numbers matter, because occupancy on its own does not answer the question.
Eleven runners busy with nothing waiting means the fan-out never asked for
more. Eleven busy with 245 jobs queued is a hard ceiling. Only the pairing
tells those apart, so the report states which of the two it observed rather
than leaving it to the reader.

Nothing is built, nothing is published, and no secrets are used.

## What each job computes

Each matrix job is one 1-degree tile of a 16x16 grid covering 44-60N,
124-108W — the western North American cordillera, which is seasonal-snow
country and happens to give exactly 256 tiles, the same as GitHub's per-run
matrix cap. `scripts/process_tile.py` runs the same detection the wider project
runs:

> Dry winter snow is nearly transparent at C band, so Sentinel-1 VV backscatter
> over a snow-covered slope sits close to the bare-ground value. As meltwater
> appears in the pack, absorption rises and backscatter falls, reaching a
> minimum around the point the pack saturates and water begins to leave it.
> Once the pack drains and thins, backscatter climbs back toward bare ground.
> The date of that seasonal minimum is taken as runoff onset.

Per tile, that runs over 64x64 pixels x 61 acquisitions (one year at the 6-day
repeat): despeckle each pixel's series with a moving median, take the seasonal
minimum, reject pixels whose dip is too shallow to be a melt signal, and report
the median onset day, its spread, and the fraction of pixels resolved.

**The backscatter series is synthetic**, generated deterministically from the
tile id rather than pulled from an archive. The probe needs hundreds of
identical, self-contained, network-free jobs, and fetching real granules would
make every job depend on an external service and its credentials. The detection
is the real algorithm; only its input is stand-in data. Because the generator
knows the onset it injected, each tile also reports the RMSE of its own
estimate — which is what `scripts/test_process_tile.py` asserts against, and
what the report prints alongside the concurrency numbers.

The work is deliberately light: about 0.4s of CPU for a whole tile. A job lasts
`hold_seconds` because it processes the tile in twelve blocks on a schedule
spanning that interval, not because the arithmetic takes that long. What the
probe needs is a *controlled hold*, not a busy CPU.

`prepare` runs the unit tests before fanning out, so a broken processor fails
the run in seconds rather than 256 times over.

## Running it

Automatic: any push (except README-only changes) runs the unit tests plus a
small 4-job / 30-second version, just enough to prove Actions works in the org.

Manual: **Actions → Concurrency test → Run workflow**, with four inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `job_count` | `256` | Matrix jobs to fan out (1–256, GitHub's per-run matrix cap) |
| `hold_seconds` | `180` | How long each job holds its runner |
| `census_minutes` | `20` | How long to keep holding before draining the queue |
| `expected_limit` | `120` | The limit you expect; used only for a feasibility note |

`hold_seconds` needs to comfortably exceed runner startup, which is ~15–30s and
is staggered across a large fan-out. With short holds the early jobs finish
before the later ones have booted, and the *in-job* overlap comes out lower
than the real limit. 120s is a reasonable floor; the 180s default leaves room
at fan-outs above 200.

`census_minutes` bounds the experiment. Once the window closes, jobs still
waiting for a runner exit immediately instead of holding one, so a low limit
gives a fast answer rather than an hours-long run. `prepare` prints how long
the fan-out would take to clear at `expected_limit` and warns when that exceeds
the window — trailing jobs draining is expected and is what bounds the run.

### Going past 256 jobs

256 matrix jobs per workflow run is a hard GitHub limit, so a single run cannot
prove a limit above 256. To probe higher, dispatch several runs at the same time
(`Run workflow` a few times in a row, or the same via the API). Each run's
report aggregates runner-level concurrency across every run whose jobs actually
overlap this run's probe window, including runs still in flight, so four
simultaneous 256-job runs measure the account-wide ceiling up to 1024.

## Reading the results

The `report` job writes two sections to the run page.

**In-job concurrency (this run)** — measured from timestamps taken inside each
job, so runner setup is excluded. This is the count of jobs actually executing
at once, and it runs a little below the runner-level number. It also prints a
timeline, where queueing shows up as a staircase rather than one solid block.

**Tile results** — how many tiles finished, the median runoff onset across
them, its range, and the detection RMSE. This is the science output rather than
a concurrency measurement; it is there so the run says what it actually did.

**Runner-level concurrency and queue depth** — measured from the Actions API
using each job's step timestamps, across every run overlapping this one. This
is the number of runners the account had allocated, and it is the number to
compare against the concurrency limit. Alongside it:

- **Peak occupancy while jobs were queued** is the number that actually bounds
  the limit. The raw peak often lands on the first rise in occupancy, before
  anything has had time to queue behind it, so the report keys its verdict on
  the highest occupancy seen *with work waiting*.
- **Queue wait median / p90 / max** shows how long jobs sat before getting a
  runner. Long waits alongside flat occupancy are the signature of a ceiling.
- An **occupancy and queue depth over time** table samples both across the
  window, so a plateau is visible rather than inferred.
- When several runs are in flight, a per-run breakdown shows whether they were
  competing for the same pool.

### Suggested check for the limit increase

One run with `job_count: 256`, `hold_seconds: 180` is enough to distinguish 60
from 120, since 256 already exceeds both. Dispatch a second run concurrently
only if you need to probe above 256.

- Peak ≈ 60, with jobs queued → still on the Team default.
- Peak ≈ 120, with jobs queued → the requested increase is live.
- Peak well below 60 *with a deep queue and long waits* → something else is
  capping the org. Worth raising with Support, and the queue-wait numbers and
  the occupancy table are the evidence to attach.
- Peak well below 60 with **no** queue → the run did not bound anything. Raise
  `job_count` or `hold_seconds` and re-run before drawing any conclusion.

Run the same thing before and after the change; the two summaries are a clean
before/after for the ticket.

## Notes on what this does and does not measure

- Concurrency limits apply per account across all repositories, so an unrelated
  workflow running at the same time will lower the peak here. `GITHUB_TOKEN`
  can only read this repository, so the aggregate cannot see the rest of the
  org — run this on an otherwise idle org.
- `strategy.max-parallel` is deliberately not set, and there is no
  `concurrency:` key anywhere in the workflow, so the only ceiling is the
  account limit itself.
- Each job's runtime is set by its pacing schedule, not by how much work it
  does, so the measurement is unchanged by making the jobs compute something
  real. Peak occupancy measured before and after that change was the same.
- The runner-level windows run from each job's first step start to its last
  step end, so runner provisioning is included — that is what the limit
  governs. A job still running is counted up to the present rather than
  truncated, so runs in flight alongside this one are not undercounted.
- API timestamps are whole seconds, and a runner freed and reused within the
  same second counts once, so the reported peak is a slight lower bound at slot
  handoff. Expect to read 119 rather than 120 on occasion.
- Jobs that were created but never reached a runner (queued, or cancelled while
  queued) are counted as queue depth only. They never occupied a runner, and
  counting them as occupancy would inflate the peak by the size of the queue.
- A wait of 30s or less counts as runner provisioning rather than queueing. A
  job still booting is not the account holding it back, and counting it as
  queued made a 4-job smoke run where all four overlapped report a ceiling of 3.

## Context

This repository exists because of a GitHub Support thread about Actions job
concurrency, and the transcripts below are kept here as the record.

The underlying project is
[egagli/global_snowmelt_runoff_onset](https://github.com/egagli/global_snowmelt_runoff_onset),
an open-science pipeline that derives global snowmelt runoff onset timing from
Sentinel-1 SAR imagery. The work is embarrassingly parallel — hundreds of
independent geographic tiles, one matrix job each on `ubuntu-latest` — so
wall-clock time is set almost entirely by how many jobs run at once.

How it played out, in short:

1. **Ticket #4631453** asked to raise the concurrent job limit on the personal
   `egagli` account (Pro plan, capped at 40). Kranthi A of GitHub Enterprise
   Support confirmed the diagnosis and explained the constraint: concurrency
   increases are applied at the organization or enterprise level, never to a
   personal account. Two paths were offered — run the project under a Team
   organization, where the standard-runner limit starts at 60 and can be raised
   to 120 on request, or move the large fan-out to self-hosted runners, which
   the hosted limits don't govern. That ticket then closed before it could be
   answered, and is now archived.
2. The organization `global-snow-processing` was created on the Team plan, and
   **ticket #4694645** was opened as a follow-up requesting 120 concurrent
   standard Linux runners for it.
3. Support tried to apply the increase and it failed, because the organization
   was empty. Kayode asked for a sample repository running a simple Actions job
   before trying again — which is exactly what this repository is.
4. Once this repository existed, the increase went through. The org limit is
   now **120 concurrent jobs** on standard GitHub-hosted Linux runners.

That last step is what the workflow here is for: the "before/after" check
described above is how the change gets confirmed from the outside, by measuring
the peak number of runners actually allocated rather than trusting the number in
the ticket.

### What the measurements actually showed

Measured peak concurrency, recomputed from the Actions API two ways (from step
timestamps, and from job-level `started_at`/`completed_at`, which is the more
generous of the two):

| Run | Jobs requested | Jobs that ever ran | Peak concurrent |
| --- | --- | --- | --- |
| [32998022145](../../actions/runs/32998022145) | 256 | 256 | 11 |
| [33007662502](../../actions/runs/33007662502) | 256 | 256 | 15 |
| [33104888353](../../actions/runs/33104888353) | 256 | 81 | 17 |

Occupancy sat dead flat for the whole 20-minute census window while 241 jobs
waited, each finishing job replaced within seconds by exactly one queued job.
That is a hard cap, not a fan-out that failed to ask for more and not an
autoscaler still ramping — given 241 waiting jobs and 20 minutes, an autoscaler
would have ramped.

The useful thing for the ticket is that **the org is not reaching the Team
baseline of 60 either**, and 15 matches no plan tier (Free orgs 20, Pro 40,
Team 60, Enterprise 180). So the 120 that Support has confirmed as applied is
probably applied correctly; something else is capping runner allocation before
that limit is ever reached.

One candidate worth ruling out: a two-day-old organization fanning out hundreds
of public-repo jobs whose entire body was `sleep 180` is close to the
fingerprint of hosted-runner abuse, which GitHub throttles independently of the
plan's concurrency limit. The probe jobs now do real per-tile work instead of
sleeping, which removes that signature at no cost to the measurement — a job's
duration is set by its pacing schedule either way.

### Transcripts

Oldest first within each ticket.

#### Request to increase GitHub Actions job concurrency limits (Actions) #4631453

> **Archived** · Eric Gagliano opened this ticket for `egagli` 3 weeks ago · 2 comments
>
> You cannot comment on an archived ticket. Instead, you can create a follow-up ticket.

<details>
<summary><strong>Eric Gagliano</strong> — 3 weeks ago (original request)</summary>

> **Please describe the issue you are experiencing with GitHub-hosted runners**
>
> Hi Github Support!
>
> I maintain an open-science research software project in the public repository
> https://github.com/egagli/global_snowmelt_runoff_onset, which produces a global
> dataset of snowmelt runoff onset timing derived from Sentinel-1 SAR imagery. The
> repository's processing pipeline runs as GitHub Actions matrix jobs on standard
> `ubuntu-latest` runners: each job executes the repo's own processing code on one
> geographic tile and writes results to external cloud storage (the pipeline and
> workflow files are all public in the repo under `.github/workflows/`).
>
> First off, I want to say that GitHub Actions has been wonderful for this
> project--it's let me build the whole pipeline in the open, where anyone can
> inspect or reproduce it, and I'm really grateful that's possible on a personal
> account.
>
> The one place I'm bumping into a ceiling: the work is embarrassingly parallel
> (hundreds of independent tiles), but my account (egagli) is on the Pro plan, so
> runs are capped at 40 concurrent jobs and the remaining matrix jobs wait in the
> Queued state. A full global processing run currently takes days of wall-clock
> time even though the jobs are fully independent.
>
> While digging into this, I saw in the usage limits documentation
> (https://docs.github.com/en/actions/reference/limits) that GitHub Support can
> increase job concurrency limits on request. Would it be possible to raise the
> total concurrent job limit for my account for standard GitHub-hosted Linux
> runners? Something like 100 would be amazing, but honestly any increase would
> make a real difference.
>
> Happy to provide any additional detail about the workflows or usage, and thank
> you very much for taking a look!
>
> All my best,
> Eric Gagliano (egagli)
>
> **Are you using a standard or a larger hosted runner?**
> Standard hosted runner
>
> **If you are using a larger hosted runner, is it registered to an enterprise or an organization?**
> Not using a larger hosted runner
>
> **What runner label(s) are you using?**
> `ubuntu-latest`
>
> **What is the URL of the workflow run?**
> https://github.com/egagli/global_snowmelt_runoff_onset/actions/runs/30924761883
>
> **What is the specific error message you are observing?**
> N/A, no error! Matrix jobs remain in the 'Queued' state until a concurrency slot
> frees up, which is expected behavior at the 40 concurrent job limit on the Pro
> plan. This ticket is a limit increase request rather than a bug report.

</details>

<details>
<summary><strong>GitHub Support</strong> (Kranthi A) — 2 weeks ago</summary>

> Hi Eric,
>
> Thanks for the kind words, and for laying this out so clearly. You have the
> diagnosis exactly right. On the Pro plan, standard GitHub-hosted Linux runners
> are capped at 40 concurrent jobs, so the rest of your matrix waits in Queued
> until a slot frees. Reference: Job concurrency limits for GitHub-hosted runners.
>
> The honest constraint here is scope. Concurrency increases for GitHub-hosted
> runners are applied at the organization or enterprise level, not to an
> individual personal account, so on your personal Pro account 40 is the ceiling
> we can offer. Given your workload is embarrassingly parallel, here are the two
> practical paths.
>
> 1. Run the project under an organization. Move the repository into a GitHub
>    organization and run the workflows there. On a Team organization the
>    standard-runner limit starts at 60, and we can raise it to as high as 120
>    concurrent jobs on request. Team is a paid plan, the cost is modest, and
>    because the repository is public it keeps using free minutes for standard
>    runners. Transferring a repository preserves its history, issues, and stars.
>    If you go this route, reply with the organization name and your target, and I
>    will get the concurrency raised for you.
>
> 2. Use self-hosted runners for the large fan-out. Self-hosted runners are not
>    governed by the GitHub-hosted concurrency limits, so they are often the best
>    fit for parallel research processing like yours. You run them on your own
>    machines or cloud instances and scale to the parallelism you need.
>    Reference: About self-hosted runners.
>
> If you let me know which direction you prefer, I will outline the safest next
> steps and how to confirm the change worked against your queued matrix.
>
> Kind Regards,
>
> Kranthi A
> Github Enterprise Support

</details>

#### Follow-up to ticket 4631453: raise org Actions job concurrency to 120 (Actions) #4694645

> **Pending** · Eric Gagliano opened this ticket for `global-snow-processing` 2 days ago · 4 comments

<details>
<summary><strong>Eric Gagliano</strong> — 2 days ago (original request)</summary>

> **Please describe the issue you are experiencing with GitHub-hosted runners**
>
> Hi GitHub Support!
>
> This is a follow-up to ticket 4631453, where Kranthi A of GitHub Enterprise
> Support very kindly worked through a concurrency question with me earlier this
> month. I was too slow getting back and the ticket closed — entirely my fault. (I
> did send a late email reply to that thread on Aug 24 before realizing the reopen
> window had passed, so apologies if a duplicate surfaces somewhere.)
>
> Quick recap: I maintain an open-science research project (public repository
> https://github.com/egagli/global_snowmelt_runoff_onset) that produces a global
> dataset of snowmelt runoff onset timing from Sentinel-1 SAR imagery. The
> processing pipeline runs as embarrassingly parallel GitHub Actions matrix jobs on
> standard `ubuntu-latest` runners, and on my personal Pro account the
> 40-concurrent-job cap meant a full global run took days of wall-clock time.
> Kranthi explained that concurrency increases are applied at the organization
> level rather than to personal accounts, and offered: "On a Team organization the
> standard-runner limit starts at 60, and we can raise it to as high as 120
> concurrent jobs on request. ... If you go this route, reply with the
> organization name and your target, and I will get the concurrency raised for
> you."
>
> We've gone exactly that route! We created the organization
> `global-snow-processing`, it's on the Team plan, and we're going to move the
> repository into it. Would yall be able to please raise the total concurrent job
> limit for standard GitHub-hosted Linux runners for the `global-snow-processing`
> organization to 120?
>
> Thank you so much — and thanks again to Kranthi for the clear guidance that got
> us here. Happy to provide any additional detail about the workflows or usage.
>
> All my best,
> Eric Gagliano (egagli, owner of global-snow-processing)
>
> **Are you using a standard or a larger hosted runner?**
> Standard hosted runner
>
> **If you are using a larger hosted runner, is it registered to an enterprise or an organization?**
> An organization
>
> **What runner label(s) are you using?**
> `ubuntu-latest`
>
> **What is the URL of the workflow run?**
> https://github.com/egagli/global_snowmelt_runoff_onset/actions/runs/30924761883
>
> **What is the specific error message you are observing?**
> N/A, no error! Matrix jobs remain in the 'Queued' state until a concurrency slot
> frees up, which is expected behavior at the 40 concurrent job limit on the Pro
> plan. This ticket is a limit increase request rather than a bug report.

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — yesterday</summary>

> Hello Eric,
>
> Thank you for contacting GitHub Support.
>
> I understand you would like to increase the concurrency of your
> global-snow-processing organization from 60 to 120
>
> I tried to increase this but it is failing. Then, I saw the organization is
> empty, kindly create a sample repository on the organization and run a very
> simple test action job and let me know (you can delete this repository later).
>
> Once you do this, let me know and I will initiate the concurrency increase
> again. If it stills fails, then, I will escalate this internally to the
> engineering team.
>
> Looking forward to hearing from you, and I am more than happy to help.
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>Eric Gagliano</strong> — 18 hours ago</summary>

> Hey Kayode,
>
> Thank you for the reply! I have created a sample repository at:
>
> https://github.com/global-snow-processing/test_actions_concurrency
>
> Thank you for your time!
>
> All my best,
> Eric

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 9 hours ago</summary>

> Hello Eric,
>
> Thank you for your response,
>
> I have now successfully update your concurrency to 120
>
> Let us know if there are further concerns on this ticket.
>
> Looking forward to hearing from you.
> Best Regards,
> Kayode

</details>
