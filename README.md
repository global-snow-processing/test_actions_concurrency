# test_actions_concurrency

Scratch repository for GitHub Support ticket work on the `global-snow-processing`
organization. Its only purpose is to put a real Actions workflow run on the
org's record so the standard-runner concurrency limit can be raised, and to
measure how many jobs actually run at once.

The increase has landed and is confirmed from the outside: the org now reaches
**120 concurrent standard Linux runners**, measured
[here](../../actions/runs/34882558802). The repository is safe to delete; it is
kept for now as the record of how that was established, and as a way to re-check
the limit if it ever looks wrong again.

## What the workflow does

`.github/workflows/concurrency-test.yml` fans out N identical matrix jobs on
`ubuntu-latest`. Each job holds a runner for a fixed number of seconds and
records the wall-clock window during which it held one. It spends that time in
ten slices with a throwaway arithmetic loop at the start of each, so a job is not
a process doing literally nothing but sleeping; the number it computes is
meaningless and costs about 7ms a slice. A final
`report` job collects those windows and computes, with a sweep over the
start/end events, both the **peak occupied runners** and the **queue depth at
the same instants**.

Both numbers matter, because occupancy on its own does not answer the question.
Eleven runners busy with nothing waiting means the fan-out never asked for
more. Eleven busy with 245 jobs queued is a hard ceiling. Only the pairing
tells those apart, so the report states which of the two it observed rather
than leaving it to the reader.

Nothing is built, nothing is published, and no secrets are used.

## Running it

The concurrency test is the only workflow here and it only ever runs on demand
— there is no push trigger, so nothing fires by accident and nothing else
competes for the account's runners while a measurement is in flight.

**Actions → Concurrency test → Run workflow**, with four inputs:

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
- Peak well below 60 *with a deep queue and long waits* → the configured limit
  is not what is binding. On an account whose billing owner is new to Actions
  this is most likely the hosted-runner trust-tier ramp described under
  [Context](#context) — the configured number is an upper ceiling, and the
  effective one climbs with sustained healthy usage. Run the fan-out again over
  the following days before concluding anything; if it does not climb, the
  queue-wait numbers and the occupancy table are the evidence to attach to a
  Support ticket.
- Peak well below 60 with **no** queue → the run did not bound anything. Raise
  `job_count` or `hold_seconds` and re-run before drawing any conclusion.

Run the same thing before and after the change; the two summaries are a clean
before/after for the ticket.

A second, independent view is worth capturing alongside it: **Settings → Actions
→ Runners → GitHub-hosted runners** shows live usage as `N/limit` while a run is
in flight ([docs](https://docs.github.com/en/enterprise-cloud@latest/actions/how-tos/manage-runners/github-hosted-runners/view-current-jobs)).
It reads the configured limit straight from the org rather than inferring it, so
a screenshot of it idle and again mid-run is the clearest evidence to attach to a
ticket — it was what moved this one forward.

## Notes on what this does and does not measure

- Concurrency limits apply per account across all repositories, so an unrelated
  workflow running at the same time will lower the peak here. `GITHUB_TOKEN`
  can only read this repository, so the aggregate cannot see the rest of the
  org — run this on an otherwise idle org.
- `strategy.max-parallel` is deliberately not set, and there is no
  `concurrency:` key anywhere in the workflow, so the only ceiling is the
  account limit itself.
- A job's duration is set by `hold_seconds`, not by how much work it does, so
  the dummy calculation does not affect what is measured.
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
- What a run measures is the *effective* ceiling the account was granted at that
  moment, which is not necessarily the number configured on the org. A new
  billing owner ramps up to its configured limit over days of real usage, so a
  single run says what you get today, not what you are entitled to.

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
4. Once this repository existed, the increase went through, and the org setting
   was confirmed at **120 concurrent jobs** on standard GitHub-hosted Linux
   runners.
5. Measurement then disagreed with the setting for three weeks. Runs peaked at
   11, 15, 17, 21 and 23 concurrent jobs while a couple of hundred sat queued —
   below the Team baseline of 60, let alone 120. Screenshots of Settings →
   Actions → Runners showed the same thing from inside the org: a progress bar
   reading 22/120 with the rest of the fan-out waiting. Support escalated.
6. Engineering's answer: **the configured 120 is an upper ceiling, not the
   binding limit.** A new billing owner starts in a low hosted-runner trust
   tier, and the effective concurrency ramps as sustained healthy usage builds —
   by one after a job completes, at most once every ten minutes. Their telemetry
   had the org's effective peak climbing 9 → 15 → 21 → 23 over Aug 25–28, with
   261 jobs delayed under the reason `expected_hosted_concurrency`. Nothing was
   misapplied and nothing was flagged; the org simply had not run enough jobs
   yet.
7. The ramp has since completed. A 256-job run on Sep 14 peaked at exactly 120
   with 136 jobs queued behind it, and the ticket is closed.

That last step is what the workflow here is for: the "before/after" check
described above is how the change gets confirmed from the outside, by measuring
the peak number of runners actually allocated rather than trusting the number in
the ticket. It was also what made the ramp visible — the setting said 120 from
the start, and only measurement showed that the org was not getting it yet.

### What the measurements actually showed

Peak concurrency across six 256-job runs, from the Actions API. Each is the
highest occupancy observed *while jobs were queued*, so every row is a ceiling
the account refused to go past rather than a fan-out that failed to ask for
more:

| Run | Date | Jobs that occupied a runner | Peak concurrent | Median queue wait |
| --- | --- | --- | --- | --- |
| [32998022145](../../actions/runs/32998022145) | Aug 26 | 256 | 11 | ~10 min |
| [33007662502](../../actions/runs/33007662502) | Aug 26 | 256 | 15 | ~10 min |
| [33104888353](../../actions/runs/33104888353) | Aug 27 | 81 | 17 | ~10 min |
| [33116538714](../../actions/runs/33116538714) | Aug 27 | 256 | 21 | 19 min |
| [33193219102](../../actions/runs/33193219102) | Aug 28 | 256 | 23 | 16 min |
| [34882558802](../../actions/runs/34882558802) | Sep 14 | 256 | **120** | 3 min |

Through Aug 28, occupancy sat dead flat for the whole 20-minute census window
while 230-odd jobs waited, each finishing job replaced within seconds by exactly
one queued job. Most of the fan-out only reached a runner after the window had
closed and so drained without holding one — 113 of 256 jobs on Aug 27, 99 on
Aug 28 — which is why the middle column counts every job as having occupied a
runner while the peak stays in the twenties. The reading recorded here at the
time was that the org was not even reaching the Team baseline of 60, that 15
matched no plan tier, and that something other than the concurrency setting was
therefore capping allocation.

That was right about the cause being elsewhere and wrong about what it was.
Engineering's explanation — a hosted-runner trust-tier ramp on a new billing
owner, climbing by one per completed job and at most once every ten minutes —
also explains why the numbers rose run over run rather than staying put, which
was visible in the table above before anyone knew what to call it. Their
telemetry (9 → 15 → 21 → 23 over Aug 25–28) tracks the measured series closely;
the Aug 28 run's own report says 23, while the progress bar quoted in the ticket
read 22 mid-run.

The Sep 14 run is the first taken after the ramp finished, and it is a different
picture entirely:

- **Peak occupancy 120**, in-job and runner-level agreeing exactly, held flat
  with 136 jobs queued behind it — so 120 is a ceiling that was reached, not a
  number that merely went unchallenged.
- All 256 jobs held a runner; none drained unheld past the census window.
- Median queue wait 189s, down from ~19 minutes, with the whole 256-job fan-out
  clearing in 9m 19s against a 20-minute window.

The occupancy trace shows the shape of a limit being met rather than approached:
2 runners at t+0, 118 by t+23s, 120 by t+70s, and 120 from there until the queue
ran dry at around t+370s. The two dips in the trace are cohort handoffs — the
whole first wave of 120 started within the same few seconds, so it also finishes
within the same few seconds — not the ceiling moving.

### Transcripts

Oldest first within each ticket. Relative timestamps are as GitHub rendered them
when each transcript was captured — the archived ticket on Aug 26, 2026, the
follow-up on Sep 14, 2026.

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

> **Closed** · Eric Gagliano opened this ticket for `global-snow-processing` 3 weeks ago · 13 comments

<details>
<summary><strong>Eric Gagliano</strong> — 3 weeks ago (original request)</summary>

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
<summary><strong>GitHub Support</strong> (Kayode) — 3 weeks ago</summary>

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
<summary><strong>Eric Gagliano</strong> — 3 weeks ago</summary>

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
<summary><strong>GitHub Support</strong> (Kayode) — 3 weeks ago (increase applied)</summary>

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

<details>
<summary><strong>Eric Gagliano</strong> — 3 weeks ago (first measurements disagree)</summary>

> Hi Kayode,
>
> Thank you for applying the increase!
>
> I've since run some tests in the sample repository, and it looks like the
> change may not have taken effect. The organization seems to be capping out
> around 15 concurrent standard Linux jobs — well below the 120 that was applied,
> and below the Team default of 60 as well.
>
> Roughly what I did: I set up a workflow that fans out a few hundred trivial
> matrix jobs on `ubuntu-latest`, each of which just sleeps for a few minutes, and
> then measured how many were actually running at the same time versus sitting in
> the queue. Occupancy stayed flat in the low teens for the whole run while the
> large majority of jobs waited, some for over twenty minutes. I repeated this
> several times across two days and got the same result each time, and the
> organization has only this one repository so nothing else was competing for
> capacity.
>
> Here's a representative run:
> https://github.com/global-snow-processing/test_actions_concurrency/actions/runs/33007662502
>
> Would you be able to confirm that the 120-job limit is actually live for
> global-snow-processing and applied to standard hosted Linux runners? And if it
> is live, could you check whether something separate is throttling the
> organization? I wondered whether newly created organizations might be subject to
> some kind of ramp-up, but that's just a guess on my part.
>
> Happy to run any further tests that would be useful, or to dispatch a run at a
> time convenient for you to watch from your side.
>
> Thanks again for your help with this,
>
> Eric Gagliano (egagli, owner of global-snow-processing)

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 3 weeks ago</summary>

> Hello Eric,
>
> Thank you for your response,
>
> That is actually a bit weird. I just re-confirmed that the concurrency is now
> capped at 120
>
> Can you confirm it is now reflecting as expected as I just reapplied the update.
>
> If you are still seeing lower value, kindly share a screenshot and I can
> investigate.
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>Eric Gagliano</strong> — 3 weeks ago (peak 21 after reapplying)</summary>

> Hi Kayode,
>
> Thanks for re-applying it. I ran another test just now and wanted to share what
> I'm seeing, since it doesn't seem to line up with the 120.
>
> I dispatched 256 jobs on `ubuntu-latest`, each one holding its runner for 180
> seconds and doing nothing else, in the global-snow-processing org that has the
> single repository test_actions_concurrency and nothing else running at the time.
> The run is here:
> https://github.com/global-snow-processing/test_actions_concurrency/actions/runs/33116538714
>
> Peak concurrency came out at 21 — it sat flat at 20 for the first twelve minutes
> or so, then 21 for the remainder. The other ~235 jobs stayed queued throughout,
> each one starting only as another finished, with a median queue wait of about 19
> minutes and a maximum of just under 21. I measured this from the Actions API
> rather than from inside the jobs, taking each job's start and end and counting
> how many overlapped at each second.
>
> For context, earlier runs over the past couple of days came out at 11, 15 and 17
> by the same method. So it moves around a bit, but everything I've measured is in
> the same range, and all of it is below the 60 I understood to be the Team
> baseline rather than just below the 120.
>
> I don't know what to make of that from the outside — I can only see how many
> runners I actually get, not what limit is being applied or why. If there's
> anything you can see from your side about what's governing runner allocation for
> this org, I'd be glad to hear it.
>
> The workflow is a single dispatch-only job that holds a runner and records when
> it did, and the README in that repo covers the method if it's useful. I'm also
> happy to kick off a run at a time that suits you if watching one live would
> help.
>
> Thanks for all your help,
> Eric

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 2 weeks ago</summary>

> Hello Eric,
>
> Thank you for your response,
>
> I appreciate the detailing in your response. I will check this internally and
> get back to you.
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 2 weeks ago (asks for the runner usage view)</summary>

> Hello Eric,
>
> I checked internally and this is bizarre as the concurrency has been updated.
>
> We were wondering if there are some other runs or other repository that was
> running jobs concurrently with your test, but, I could only see a repository
> from our end. Can you kindly validate if there are no other repositories running
> jobs at the same time?
>
> Also, I got an update internally that you should to monitor how GitHub-hosted
> runners are processing jobs in your organization or enterprise, and share full
> screenshots of what you see in the progress bar. You can follow this
> documentation:
> https://docs.github.com/en/enterprise-cloud@latest/actions/how-tos/manage-runners/github-hosted-runners/view-current-jobs
>
> I look forward to hearing from you and I hope we get to the root of this
> together.
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>Eric Gagliano</strong> — 2 weeks ago (22/120 in the org's own progress bar)</summary>

> Hi Kayode,
>
> Thanks for checking internally, and for the pointer to the runner usage view —
> that turned out to be the useful thing to look at. I've attached two screenshots
> from Settings → Actions → Runners → GitHub-hosted runners.
>
> The first is taken immediately before I started the test: All jobs usage 0/120,
> with Linux 0, Windows 0, macOS 0, and "There are currently no running jobs".
> That's the org sitting completely idle.
>
> The second is about five minutes into a 256-job run: All jobs usage 22/120, with
> Linux 22, Windows 0, macOS 0, and the active jobs list showing Concurrency test
> / spin N on ubuntu-latest from global-snow-processing/test_actions_concurrency.
> It has been sitting at 22 for a while now, with the remaining ~212 jobs queued
> behind it. Run in question:
> https://github.com/global-snow-processing/test_actions_concurrency/actions/runs/33193219102
>
> On your question about other repositories or concurrent jobs — I don't believe
> anything else is competing. The org has the single repository you can see, and
> its only workflow runs on manual dispatch alone: there's no push trigger, no
> schedule, and nothing else in the org, so nothing starts unless I start it. The
> 0/120 in the first screenshot is what the org looks like when I'm not running
> the test, and the second screenshot shows all 22 in use are Linux with nothing
> on Windows or macOS.
>
> The part I find hard to interpret is that the progress bar reads 22/120. So the
> 120 is applied and visible from my side too — it's just that usage plateaus
> around 20-22 and the remaining ~98 slots stay unused while a couple of hundred
> jobs wait in the queue. I also measured the same run independently from the
> Actions API, counting how many jobs overlapped at each second, and got 22 — the
> same number the progress bar shows, so at least the two views agree.
>
> Happy to leave a run going or start one at a specific time if it's easier to
> observe from your side while it's live.
>
> Thanks,
>
> Eric
>
> *Attachments: `test_concurrency_runner_settings_before.png`,
> `test_concurrency_runner_settings_after.png`*

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 2 weeks ago</summary>

> Hello Eric,
>
> Thank you for your response and for sharing necessary details.
>
> I have now escalated this to your engineering team to take a look and I will
> give you a timely update once I get one.
>
> Thank you for your patience!
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>GitHub Support</strong> (Kayode) — 3 days ago (engineering's answer: trust-tier ramping)</summary>

> Hello Eric,
>
> Thank you for your patience,
>
> I got a response from the engineering team:
>
> > The 120 you set is an upper ceiling, not the binding limit.
> > global-snow-processing first used Actions on 2026-08-25, and new billing
> > owners start in a low hosted-runner trust tier whose effective concurrency
> > auto-ramps as sustained, healthy usage builds — telemetry shows the org's
> > effective peak climbing 9→15→21→23 over Aug 25–28 with 261 jobs delayed under
> > reason `expected_hosted_concurrency`. That trust-tier ramp, not the org
> > setting, is why you plateau near ~20 while jobs queue. Raising the effective
> > ceiling requires a trust-tier uplift from the hosted-compute team rather than
> > a further change to the org concurrency number.
>
> In simple terms;
> Free accounts, as well as paid accounts created less than 7 days ago (except
> top-tier Enterprise accounts), go through a process called ramping.
>
> At the moment, I can see that the account's concurrency is 23. The concurrency
> limit increases by 1 after a job completes, with increases occurring at most
> once every 10 minutes.
>
> This ramping process is in place to help protect our resources from potential
> abuse.
>
> That said, the concurrency will get there soon enough.
>
> Kindly run more jobs and let me know if there are further concerns or the
> concurrency is increasing as more jobs are being run.
> Best Regards,
> Kayode

</details>

<details>
<summary><strong>Eric Gagliano</strong> — 7 minutes ago (closing the ticket)</summary>

> Excellent, this was very helpful. It seems to be working now. Thank you very
> much for all your help!

</details>
