# Plan for enumerating Sp(8,2)-conjugacy classes of subgroups in GAP

## Scope

The target is one representative for each subgroup of `Sp(8,2)` up to
conjugacy by `Sp(8,2)` itself. This is the same equivalence relation used by
`ConjugacyClassesSubgroups(G)` when `G := Sp(8,2)`, but the direct command is
not a viable `Sp(8,2)` implementation strategy because it tries to hold too
much global subgroup-lattice data in one GAP process.

The best practical strategy is a checkpointed maximal-subgroup descent in a
small faithful permutation representation. GAP's global `Lattice` machinery
remains useful as a conceptual reference and for local validation, but it
should not be the primary `Sp(8,2)` driver.

## Recommended strategy

Use the following workflow.

1. Construct `G := Sp(8,2)` only as the source matrix group.
2. Build a faithful permutation image
   `P := Image(SmallerDegreePermutationRepresentation(Image(IsomorphismPermGroup(G))))`.
3. Enumerate recursively by maximal subgroups:
   start with `P`, compute `MaximalSubgroupClassReps(H)` for each unprocessed
   representative `H`, and add each returned maximal subgroup to a global
   database only if it is new up to conjugacy in the top group `P`.
4. Persist every processed node and every discovered child to disk before
   moving on to the next node.
5. Use many independent GAP worker processes for different queued subgroup
   representatives, then merge their outputs by top-level conjugacy in `P`.
6. Convert final representatives back to matrix subgroups of `Sp(8,2)` only at
   export time.

This avoids the main failure mode of the brute-force script: a single command
attempting to build and retain the entire subgroup classification at once.

## Resource utilization plan

The production algorithm should be engineered to use the 28-core machine
deliberately. The earlier sequential queue sketch is only the mathematical
kernel; it is not the scheduler.

The scheduler should use breadth-first or round-based frontier expansion,
because that exposes many independent subgroup representatives at once. A
depth-first search leaves too many cores idle near the top of the tree.

Use two worker pools:

- **ordinary pool**: 22 to 26 GAP workers for normal
  `MaximalSubgroupClassReps(H)` jobs;
- **heavy pool**: 1 or 2 GAP workers for known hard jobs, initially the two
  orthogonal branches and any later job whose previous attempt exceeded a
  runtime or memory threshold;
- **coordinator/merge capacity**: reserve at least 1 core for merge and
  checkpoint tasks, plus OS headroom.

This gives a concrete first launch policy for the currently visible machine:

```text
ordinary workers: 24
heavy workers:     1
merge/coordinator: 1
spare/OS:          2
total:            28 logical CPUs
```

If the ordinary frontier is smaller than 24 jobs, fill the remaining cores
with merge-by-order tasks, validation closures, table-of-marks processing for
the orthogonal branches, or export/fingerprint computation. The first two
levels are naturally thin, but after the 11 top-level maximals are expanded the
frontier should be large enough for full CPU occupancy.

The RAM policy must be enforced by an admission controller, not just monitored
after the fact. At startup, record `nproc`, `free -m`, and any cgroup memory
limit. In the current run environment `free -h` reports 58 GiB total RAM,
56 GiB available RAM, and 15 GiB swap, with no visible cgroup memory limit.
Use `RAM_TOTAL_GB := 58` for scheduler arithmetic unless a later run reports a
different visible limit.

Keep at least 8 to 10 GB free for the OS, file cache, shell, and merge spikes.
With 58 GiB visible, the scheduler should use `RAM_WORK_GB := 48` initially.
Every job must declare a memory reservation before launch, and the scheduler
must enforce:

```text
sum(memory reservations of running jobs) + merge_reserve_gb <= RAM_WORK_GB
```

Do not count on observed RSS alone. A light job that normally uses 155 MB
should still reserve its hard cap, because a deeper branch may allocate much
more suddenly.

Use GAP's hard workspace limit for every worker. The relevant command-line
option is `-K`; `-o` only warns and is not a safety limit. Run workers through
`timeout` and `/usr/bin/time -v`, and use `--quitonbreak` so GAP errors do not
leave a process sitting in an interactive break loop:

```text
timeout <wall-limit> /home/valbert/gap-4.15.1/gap -q --quitonbreak -K <workspace-cap> <script.g>
```

Suggested initial caps:

| class | GAP `-K` cap | wall limit | scheduler memory reservation |
|---|---:|---:|---:|
| light | 1g | 30 min | 1 GB |
| medium | 4g | 3 h | 4 GB |
| heavy | 16g | 12 h | 16 GB |
| orthogonal fallback | 24g | 24 h | 24 GB |

These caps are deliberately conservative. If a job hits `-K` or the wall
limit, mark it incomplete, keep its representative in the database, and
requeue it in the next heavier class or route it to the orthogonal fallback.
Never allow an uncapped GAP job in the production run.

Measured small-worker footprints on this machine were modest:

| job | max RSS |
|---|---:|
| build degree-120 `P` and compute top maximals | 145 MB |
| compute maximals inside top class 1 | 155 MB |
| compute maximals inside top class 11 | 159 MB |
| 24 simultaneous class-1 workers, each | about 155 MB |
| 24 simultaneous mixed light workers, each | about 145-161 MB |

These numbers justify many ordinary workers, but they are not safe upper
bounds for deeper or harder branches. The 24-worker smoke test verifies that
the ordinary pool can keep the CPUs busy for light jobs without stressing RAM,
but it does not justify running many medium or heavy jobs at once. Every
worker should be launched under `/usr/bin/time -v` or equivalent logging, and
the scheduler should classify future jobs by observed peak RSS and runtime:

| class | initial concurrency | memory assumption | action |
|---|---:|---:|---|
| light | up to 24-26 | 1 GB each | batch many ids per worker |
| medium | 8-12 | 2-4 GB each | run fewer per batch |
| heavy | 1-2 | 10-20 GB each | isolate and checkpoint aggressively |
| failed/OOM | 0 | unknown | reroute through special handling |

For the initial 58 GiB run, the safest full-machine mix is:

```text
24 light jobs at 1 GB each       = 24 GB
 1 heavy job at 16 GB            = 16 GB
merge/coordinator reservation    =  4 GB
headroom outside RAM_WORK_GB     = 10 GB
total planned pressure           = 54 GB of 58 GiB
```

If a second heavy job is launched, reduce ordinary workers accordingly. For
example, two 16 GB heavy jobs leave room for at most 12 light jobs plus merge
reserve under the same headroom policy.

Do not let independent GAP jobs all rebuild large state for one tiny task.
After the prototype works, change `sp8_worker_maximals.g` into a batch worker
that receives several subgroup ids, builds the degree-120 top group once, then
processes ids until it reaches a time limit, memory threshold, or empty shard.
Use small batches for heavy jobs and larger batches for light jobs.

Keep the in-memory state of every process bounded:

- the scheduler keeps only metadata: id, order, fingerprint, status, parent
  ids, job class, runtime, and peak RSS;
- each representative's GAP generators live in a separate file under a
  content-addressed or id-addressed path;
- workers write raw child records to a temporary file and atomically rename it
  when complete;
- workers write an explicit success sentinel only after all child records are
  flushed; the coordinator ignores output without this sentinel even if the
  shell exit status is ambiguous;
- the coordinator uses `flock` or a single-writer rule for database commits;
- no worker mutates the global representative database directly.

Add backpressure. If unmerged raw child output exceeds either 50,000 records
or 2 GB on disk, pause expansion workers and run merges until the backlog is
below the threshold. This prevents a fast duplicate-generating layer from
turning into a disk and merge-memory problem.

The merge step also needs parallelism. Since conjugate subgroups have the same
order, raw child records can be partitioned by subgroup order. Run independent
`sp8_merge_bucket.g` jobs over order buckets against a read-only snapshot of
the representative database. Each bucket emits proposed new representatives
and incidence rewrites; a single coordinator then commits them atomically. This
uses additional cores when the expansion frontier is thin and prevents one
serial merge from becoming the next bottleneck.

Merge workers must also be memory bounded. They should load only one order
bucket at a time, then sub-bucket by fingerprint. If an order bucket is too
large, split it by fingerprint hash and run several merge jobs. The final
conjugacy check still uses `IsConjugate(P, H, R)`, but it should only be
called on candidates that pass the cheap invariant filters.

The cheap filters should include both the coarse fingerprint and a cached
`detail_key` for all subgroups of order at most 4096. The current detail key is
`detail_v2`, consisting of center size, derived-subgroup size, the collected
orbit-length spectrum on unordered pairs of degree-120 points, and the
collected element-order spectrum. There should be no lower order cutoff: the
small buckets such as 64, 96, 128, 192, 256, and 384 are precisely where a
missing detail key causes millions of avoidable `IsConjugate` calls. For these
orders, enumerating elements and pair orbits is cheap compared with conjugacy
testing. The upper cutoff at 4096 can stay until a class-based element-order
spectrum is implemented for larger groups.

Store `detail_key` in both representative JSON files and raw child records.
Python should use it before launching GAP: when all candidates in a bucket have
nonempty detail keys, pass only existing representatives with matching
`detail_key` or unknown legacy keys. GAP merge workers must still recompute
missing keys for legacy records and bucket both existing and newly accepted
groups by detail key so the accepted-list scan does not grow quadratically
inside one large merge job.

The intended steady state is therefore:

1. launch worker batches until either CPU slots or RAM budget is exhausted;
2. collect raw children and worker resource logs;
3. run parallel order-bucket merges;
4. commit a new representative snapshot and frontier;
5. update job weights from observed runtime/RSS;
6. repeat.

This plan should keep the 28 cores busy whenever the frontier has enough jobs
while still protecting the run from a single uncontrolled GAP process consuming
the whole RAM budget.

The scheduler should refuse to start if any of these preflight checks fail:

- fewer than 28 logical CPUs are visible, unless the worker counts are reduced;
- less than 50 GiB RAM is visible for the full run, unless `RAM_WORK_GB` and
  concurrency are reduced;
- `/home/valbert/gap-4.15.1/gap` does not support `-K`;
- `/usr/bin/time`, `timeout`, and `flock` are unavailable;
- the work directory has less than 50 GB free disk space;
- a stale lock indicates another coordinator is already committing a snapshot.

## Persistent run state and learned data

The implementation must be resumable by construction. Every script should
write down anything it learns before moving to the next expensive step, and no
script should rerun a completed expensive GAP subprocess if its output artifact
exists, has a success sentinel, and matches the current run manifest.

Use an append-friendly work directory such as
`scripts/sp_subgroups/sp8/work_sp8/<run_id>/` with this structure:

```text
manifest.json
group_model.g
snapshots/
  reps_000000.jsonl
  reps_000001.jsonl
  frontier_000001.jsonl
  incidence_000001.jsonl
reps/
  rep_<id>.g
  rep_<id>.json
jobs/
  pending.jsonl
  running.jsonl
  completed.jsonl
  failed.jsonl
raw_children/
  job_<job_id>.children.g
  job_<job_id>.children.jsonl
merge/
  bucket_<order>_<hash>.proposals.jsonl
  bucket_<order>_<hash>.log
caches/
  conjugacy_tests.jsonl
  fingerprints.jsonl
  normalizers.jsonl
detail_backfill/
  batch_<timestamp>_<index>/
    input.g
    details.jsonl
    SUCCESS
  runs.jsonl
logs/
  scheduler.jsonl
  worker_<job_id>.time
  worker_<job_id>.stdout
  worker_<job_id>.stderr
validation/
  sp6_validation.json
  closure_checks.jsonl
```

The exact serialization can be GAP-readable records, JSON lines, or both, but
the division of responsibility should remain the same: generator data in
`reps/`, queue and status data in `jobs/` and `snapshots/`, expensive learned
facts in `caches/`, and raw unmerged worker discoveries in `raw_children/`.

### Manifest

`manifest.json` is the run contract. It should contain:

- GAP binary path and version;
- command-line flags used for workers, including `--quitonbreak` and `-K`;
- construction method for the degree-120 group `P`;
- order of `Sp(8,2)` and degree/order checks for `P`;
- `RAM_TOTAL_GB`, `RAM_WORK_GB`, worker counts, wall limits, and per-class
  workspace caps;
- script versions or git commit hash if available;
- timestamped run id;
- schema version for every record type.

If the manifest changes in a way that can affect mathematical output, start a
new run directory or explicitly write a migration record. Do not silently mix
artifacts from incompatible runs.

### Representative records

Every discovered subgroup class should have two files:

- `rep_<id>.g`: GAP code defining the subgroup generators in the degree-120
  permutation group;
- `rep_<id>.json`: metadata.

The metadata should include:

- stable id;
- order;
- generator file path;
- parent ids and first-discovery job id;
- fingerprint values;
- status: `new`, `queued`, `processing`, `processed`, `failed`, or
  `needs_special_handling`;
- job class: `light`, `medium`, `heavy`, or `orthogonal_fallback`;
- number of maximal subgroup classes found inside it, once known;
- timestamps for first seen, queued, processing start, processing finish;
- last failure reason, timeout, peak RSS, and workspace cap if any.

Use stable ids derived from a canonical fingerprint plus a sequence number
assigned by the coordinator. The id does not have to decide conjugacy; it only
has to make records refer to one another safely. Mathematical equality is
still decided by the merge step with `IsConjugate(P, -, -)`.

### Job records

Each expensive subprocess gets a job record before launch. The record should
include:

- job id;
- job type, such as `maximals`, `merge_bucket`, `validation`, or
  `orthogonal_lift`;
- input representative ids or bucket ids;
- exact command line;
- workspace cap, wall limit, RAM reservation, and worker pool;
- status: `pending`, `running`, `completed`, `timeout`, `workspace_limit`,
  `nonzero_exit`, `killed`, or `needs_retry`;
- start/end timestamps;
- exit status;
- peak RSS from `/usr/bin/time -v`;
- output artifact paths;
- success sentinel path and checksum.

A worker may write partial files, but the coordinator must ignore them unless
the job status is `completed`, the success sentinel exists, and the checksum
matches. If a job times out or hits `-K`, keep the job record and stderr/stdout
logs, but do not mark the representative `processed`.

### Raw child records

For a completed `MaximalSubgroupClassReps(H)` job, record all learned children
even before global deduplication:

- parent representative id;
- local child number;
- child order;
- child generator file or inline generator reference;
- child fingerprint;
- worker job id;
- whether GAP reported the maximal-subgroup computation complete;
- checksum of the raw child file.

This makes the expensive maximal-subgroup call reusable. If the merge step
crashes, rerun only the merge over existing raw child records, not the worker
that computed them.

### Merge and conjugacy cache

The merge step should persist every final top-level conjugacy decision:

- pair of candidate ids or raw-child ids;
- order and fingerprint bucket;
- result of `IsConjugate(P, A, B)`;
- optional conjugating element if GAP returns or can cheaply compute one;
- timestamp and merge job id.

The cache is not a substitute for correctness, but it prevents repeated
`IsConjugate` calls after a crash or during an audit merge. On restart, the
merge worker should load only the relevant order/fingerprint cache entries.

Every merge bucket should produce a proposal file with:

- existing representative ids considered;
- raw children accepted as new representatives;
- raw children identified with existing representatives;
- incidence rewrites from parent id to global child representative id;
- cache entries added;
- bucket completion sentinel.

The coordinator commits a proposal by writing a new snapshot number. Do not
edit an existing committed snapshot in place.

### Progress snapshots

At the end of each scheduler round, write a complete progress snapshot:

- total representative count;
- counts by status and job class;
- frontier ids by job class;
- processed ids;
- failed and retry ids;
- raw child backlog size;
- merge bucket backlog size;
- total jobs completed, timed out, failed, and retried;
- maximum observed RSS by job class;
- list of hard branches still unresolved;
- latest committed snapshot id.

This snapshot should be enough to answer "how far did it get?" without
examining stdout logs. It should also be enough for `sp8_recover.g` to rebuild
the frontier.

### Atomicity and recovery rules

Use a write-then-rename discipline:

1. write to `*.tmp`;
2. flush and close;
3. write a checksum or success sentinel if appropriate;
4. atomically rename to the final path;
5. only then update the coordinator snapshot.

The coordinator is the only process allowed to mutate committed snapshots.
Workers and merge buckets write proposals and raw data only. Use `flock` around
snapshot commits so that a second coordinator cannot interleave writes.

On restart, `sp8_recover.g` should:

1. read the latest complete snapshot;
2. discard `*.tmp` files and outputs without success sentinels;
3. mark stale `running` jobs as `needs_retry` or heavier-class retries;
4. retain completed raw child files and merge proposal files;
5. rebuild the frontier from representatives not marked `processed`;
6. verify that every `processed` representative has a completed maximals job
   and a recorded maximal-subgroup count;
7. verify that every incidence edge points to an existing global
   representative id.

This is the key rule: a failed, killed, or out-of-memory step should lose at
most the work of that one subprocess, never the completed outputs of earlier
subprocesses.

## Correctness argument

The recursive maximal-subgroup algorithm is complete provided every call to
`MaximalSubgroupClassReps(H)` succeeds and the merge step uses a final
`IsConjugate(P, A, B)` test before identifying two subgroups.

For any proper subgroup `K < P`, there is a maximal subgroup `M < P` with
`K <= M`. The top-level call returns one representative of every `P`-conjugacy
class of maximal subgroups, so after conjugating `K` if necessary, `K` lies in
one of the queued top-level maximal representatives. Repeating the same
argument inside that representative gives a maximal chain from `P` down to a
`P`-conjugate of `K`. Induction on subgroup order then shows that the recursive
descent reaches every `P`-conjugacy class of subgroups.

Deduplication may use fingerprints as filters, but it must only merge two
records after `IsConjugate(P, A, B)` returns `true`. This prevents silent loss
of subgroup classes.

## Why not use global `Lattice(P)` first?

The notes in `sp8z2_gap_subgroup_strategies.md` identify `Lattice` and cyclic
extension as GAP's built-in all-subgroups route, especially when supplied with
perfect-subgroup seeds. That route is mathematically natural, but for this
specific computation it is a poor first engineering choice:

- it keeps a large global lattice object live in one process;
- it is not automatically parallel across the 28 cores available here;
- it is harder to checkpoint in a useful intermediate state;
- an incomplete `perfectSubgroups` seed list risks an incomplete result.

The maximal-descent plan still uses GAP's subgroup-classification machinery,
but it confines each hard calculation to one subgroup at a time and makes the
outer search restartable.

## GAP script outline

Do not put the full algorithm in one monolithic script. Use small scripts with
explicit data files.

### `sp8_common.g`

Shared definitions:

- `BuildTopGroup()`: returns a record containing the matrix group `G`, the
  degree-120 permutation group `P`, and the composed homomorphism `phi: G -> P`.
- `Fingerprint(H)`: returns only conjugacy-invariant filters, such as order,
  orbit lengths on `[1..120]`, solvability, abelian invariants when applicable,
  derived subgroup order, and composition-factor orders.
- `IsNewClass(P, H, reps_by_order)`: finds candidates with compatible
  fingerprints and then runs `IsConjugate(P, H, R)`.
- serialization helpers for records whose generators are printed as GAP
  permutations.

Keep `StructureDescription(H)` out of the hot path. It is useful for reports,
but it is too slow and too nonessential for queue processing.

### `sp8_init.g`

Responsibilities:

- build `P` and assert `Order(P) = Order(Sp(8,2))`;
- assert the kernel of the composed map from `Sp(8,2)` to `P` is trivial;
- create a new work directory, for example `scripts/sp_subgroups/work_sp8/`;
- write `manifest.json` and `group_model.g`;
- write the initial representative record for `P`;
- compute and write the 11 top-level maximal subgroup representatives;
- write snapshot `000000` containing the top group, top maximals, initial
  frontier, and zero completed jobs.

### `sp8_worker_maximals.g`

Input: one subgroup record id in the prototype; a batch of subgroup ids in the
production scheduler.

Responsibilities:

- read the manifest and refuse to run if the script settings do not match the
  run directory;
- before computing, check whether the representative already has a completed
  maximals job with a success sentinel and matching checksum; if so, exit
  successfully without recomputing;
- read the subgroup representative `H`;
- compute `MaximalSubgroupClassReps(H)`;
- for each maximal subgroup `M`, write a raw child record containing parent id,
  order, generators, fingerprint, and `detail_key`;
- write the complete raw child file, maximal-subgroup count, and checksum;
- write a success sentinel only after all raw child data is flushed;
- write a worker log recording runtime, peak RSS, success/failure, and whether
  the job should be reclassified as light, medium, or heavy.

The worker should not decide global uniqueness on its own. That should be done
by a merge script so that all `IsConjugate(P, -, -)` calls are centralized and
repeatable.

### `sp8_merge_frontier.g`

Responsibilities:

- read all raw child records not yet merged;
- skip raw child files that do not have a success sentinel and matching
  checksum;
- bucket candidates by order and safe fingerprints;
- consult `caches/conjugacy_tests.jsonl` before running an `IsConjugate` test
  already completed in a previous merge attempt;
- run final top-level `IsConjugate(P, H, R)` tests against existing
  representatives;
- add genuinely new classes to the representative database and frontier queue;
- record parent-child incidence after replacing each child by its global
  representative id;
- write merge proposals and conjugacy-cache additions before coordinator
  commit.

For the first full run, keep an audit mode that ignores fingerprints except for
order. Once this matches the fingerprinted merge on several rounds, enable the
faster filtering by default.

For production, split this into order-bucket merge jobs. A merge worker should
only compare candidates of one order, write proposed additions to a temporary
file, and leave the final database mutation to a single coordinator. That keeps
parallel merges deterministic.

### `sp8_detail_batch.g`

Responsibilities:

- read a batch of representative or raw-child group files;
- compute `SP8_DetailKeyString` with the same `detail_v2` helper used by
  workers and merges;
- write one JSONL result per input record plus a success sentinel;
- leave all JSON metadata mutation to the Python coordinator.

The coordinator command `backfill-details` should use this script to migrate
legacy runs. It must refuse to write while the master orchestrator is live
unless explicitly forced, split work into bounded GAP batches, apply completed
batch output atomically, and be safely rerunnable. On restart, it first applies
any prior successful `detail_backfill/batch_*/details.jsonl` output, then skips
records that already have `detail_key`.

### `sp8_run_round.g` or shell wrapper

Responsibilities:

- load the latest progress snapshot and job table;
- select unprocessed frontier ids;
- assign ids to light, medium, and heavy queues using previous runtime/RSS
  observations;
- launch enough independent GAP workers to fill the ordinary pool;
- launch at most one or two heavy workers;
- avoid launching a job if its estimated RSS would exceed the current RAM
  budget minus headroom;
- run order-bucket merge jobs after each expansion batch;
- commit a new snapshot only after all selected merge proposals have been
  validated;
- write a scheduler log with CPU slots used, RAM budget, worker exits, and
  queue sizes.

This can be a GAP script using `Exec`, a short shell script, or GNU `parallel`.
The mathematical algorithm should remain in GAP; the wrapper only schedules
jobs.

### `sp8_preflight.sh`

Responsibilities:

- verify that `/home/valbert/gap-4.15.1/gap` starts and advertises `-K`;
- verify that `--quitonbreak` makes an intentional GAP error exit nonzero;
- record GAP version, `nproc`, `free -h`, `/proc/meminfo`, and disk space;
- verify that `/usr/bin/time`, `timeout`, and `flock` are available;
- fail if fewer than 50 GiB RAM or 50 GB free disk are visible unless an
  explicit reduced-resource mode is selected;
- write the selected `RAM_WORK_GB`, worker counts, and per-class `-K` caps to
  the run manifest;
- refuse to start a new run in an existing work directory unless recovery has
  verified the latest committed snapshot.

### `sp8_recover.g`

Responsibilities:

- scan the work directory after an interrupted run;
- discard incomplete temporary files;
- rebuild the frontier from representatives whose status is not `processed`;
- preserve failed/heavy jobs with their last exit reason and resource log;
- preserve completed worker outputs and raw children so their GAP subprocesses
  are not rerun;
- preserve completed merge proposals and conjugacy-cache entries so merge
  buckets are not recomputed unnecessarily;
- verify that the latest committed representative snapshot has no duplicate
  ids and no dangling parent-child incidence records.

### `sp8_status.g`

Responsibilities:

- read the latest progress snapshot without starting GAP subgroup
  computations;
- report representative counts by status, job class, subgroup order, and
  frontier depth;
- report processed, queued, running, failed, timed-out, and retry job counts;
- report raw child and merge backlogs;
- report unresolved heavy branches, especially top classes 8 and 9;
- report maximum observed runtime and peak RSS by job class;
- report the latest completed snapshot id and whether recovery is required.

This script is how a user should answer "how far did the algorithm get?" after
a long run or failure. It must not recompute subgroup data.

### `sp8_validate.g`

Validation checks:

- rerun the same recursive maximal-descent method for `Sp(4,2)` and `Sp(6,2)`
  and compare the number of classes with `ConjugacyClassesSubgroups`;
- for the final `Sp(8,2)` database, verify that every processed representative
  has all its maximal subgroups represented in the database up to `P`-conjugacy;
- rerun a slow audit merge on selected order buckets using only `IsConjugate`;
- verify that `PreImage(phi, H)` maps final permutation representatives back to
  matrix subgroups with the same order.

### `sp8_export.g`

Responsibilities:

- write one final table sorted by subgroup order;
- include order, normalizer order or class size when computed, generators in
  permutation form, and optional matrix generators;
- write summary counts by order and, for small orders, `IdSmallGroup`.

Compute expensive descriptive fields only at export time.

## Proposed GAP algorithm

The mathematical kernel is:

```gap
G := Sp(8,2);;
iso1 := IsomorphismPermGroup(G);;
P255 := Image(iso1);;
iso2 := SmallerDegreePermutationRepresentation(P255);;
P := Image(iso2);;
phi := CompositionMapping(iso2, iso1);;

seen := [ P ];;
queue := [ P ];;

while Length(queue) > 0 do
    H := Remove(queue, 1);;
    maximal_reps := MaximalSubgroupClassReps(H);;

    for M in maximal_reps do
        if IsNewUpToConjugacyInTopGroup(P, M, seen) then
            Add(seen, M);;
            Add(queue, M);;
        fi;
        RecordIncidence(H, M);;
    od;

    MarkProcessed(H);;
od;
```

The production implementation differs from this sketch in two ways: it writes
after each step, and it distributes `MaximalSubgroupClassReps(H)` calls across
worker processes.

The production scheduler should instead look like:

```text
frontier := [ top_group_record ]
while frontier is nonempty:
    jobs := select a RAM-safe batch from frontier
    run ordinary and heavy GAP workers in parallel
    collect raw child records and resource logs
    merge raw children in parallel by subgroup order
    coordinator commits new representatives and next frontier
    update job weights from observed runtime and RSS
```

This is the version intended to use the 28 cores.

## Test results on this machine

All tests below used `/home/valbert/gap-4.15.1/gap`, GAP 4.15.1.

### Resource preflight

The current shell reports:

```text
CPU: 28 logical CPUs
RAM: 58 GiB total, 56 GiB available
Swap: 15 GiB total, unused
Disk at work directory: 164 GB available
GAP hard workspace cap: -K / --limitworkspace available
GAP --quitonbreak: intentional error exits nonzero
Required helpers: /usr/bin/timeout and /usr/bin/flock available
```

This is enough for the proposed initial 24-light-plus-1-heavy worker mix.

### Faithful permutation representation

The default matrix group has the expected order:

```text
Order(Sp(8,2)) = 47377612800
```

`IsomorphismPermGroup(Sp(8,2))` produced a faithful degree-255 permutation
image. Applying `SmallerDegreePermutationRepresentation` produced a faithful
degree-120 image:

```text
P degree=120 order=47377612800 kernel=1
```

A pullback test also succeeded:

```text
sample image subgroup order=2 pullback order=2 image-back order=2
image-back equals H=true
```

This verifies that internal permutation representatives can be converted back
to matrix subgroups of `Sp(8,2)`.

### Top-level maximal subgroup classes

In the degree-120 image, `MaximalSubgroupClassReps(P)` returned 11 top-level
maximal subgroup classes quickly:

| class | order | index | generators |
|---:|---:|---:|---:|
| 1 | 185794560 | 255 | 4 |
| 2 | 8847360 | 5355 | 6 |
| 3 | 4128768 | 11475 | 6 |
| 4 | 20643840 | 2295 | 3 |
| 5 | 8709120 | 5440 | 4 |
| 6 | 1036800 | 45696 | 3 |
| 7 | 1958400 | 24192 | 3 |
| 8 | 348364800 | 136 | 3 |
| 9 | 394813440 | 120 | 3 |
| 10 | 2448 | 19353600 | 2 |
| 11 | 3628800 | 13056 | 3 |

This verifies that the first recursion layer is available without invoking the
global subgroup lattice.

### First-generation branch timings

Direct `MaximalSubgroupClassReps` calls on every tested top-level maximal
representative except classes 8 and 9 were fast:

| top class | subgroup order | maximal classes inside it | runtime |
|---:|---:|---:|---:|
| 1 | 185794560 | 10 | 0.593 s |
| 2 | 8847360 | 10 | 0.338 s |
| 3 | 4128768 | 8 | 0.193 s |
| 4 | 20643840 | 7 | 0.163 s |
| 5 | 8709120 | 10 | 0.515 s |
| 6 | 1036800 | 8 | 0.297 s |
| 7 | 1958400 | 8 | 0.662 s |
| 10 | 2448 | 5 | 0.041 s |
| 11 | 3628800 | 8 | 1.253 s |

Top classes 8 and 9 have orders matching `GO(1,8,2)` and `GO(-1,8,2)`,
respectively. Direct calls on these two inherited subgroup embeddings did not
finish within a 180 second probe. Applying
`SmallerDegreePermutationRepresentation` to each branch did not reduce the
positive branch below degree 120 and did not make the maximal-subgroup call
finish within the same 180 second limit.

Standard orthogonal-group probes also timed out at 180 seconds for
`MaximalSubgroupClassReps`:

```text
GO( 1,8,2): order=348364800
GO(-1,8,2): order=394813440
GO( 1,8,2) smaller permutation degree=120
GO(-1,8,2) smaller permutation degree=119
```

So the hard branch fallback should not assume that merely switching to the
standard `GO` constructors solves the problem. The standard constructors are
still useful for recognition, derived-subgroup checks, and possible explicit
isomorphisms if subgroup data are imported from GAP libraries.

### Resource-control probes

The hard workspace cap was tested on a normal branch:

```text
gap -q -K 1g
top class 1 maximal-subgroup job: exit 0, max RSS 155 MB
```

Thirty-second capped probes of the hard orthogonal branches did not finish,
but also did not show runaway memory before the timeout:

```text
top class 8 with -K 2g and timeout 30s: max RSS 319 MB, timed out
top class 9 with -K 2g and timeout 30s: max RSS 237 MB, timed out
```

Two ordinary-pool concurrency probes were run:

```text
24 simultaneous class-1 workers: all exit 0, about 155 MB RSS each
24 simultaneous mixed light workers from top classes 1-7,10,11:
  all exit 0, 145-161 MB RSS each, 2.63-4.28 s wall time
```

These tests support using a 24-worker ordinary pool for light jobs. They do
not remove the need for `-K`, wall limits, and admission control on deeper
branches.

### Complete small-case verification

A temporary in-memory prototype of the recursive maximal-descent algorithm was
run for `Sp(6,2)`. It used top-level conjugacy in `Sp(6,2)` for deduplication.
The result matched GAP's direct subgroup-class calculation:

```text
Sp(6,2) direct classes=1369 recursive-max seen=1369 match=true
```

This is the main correctness smoke test: the algorithmic pattern reproduces
the known direct result at the largest dimension where the brute-force command
is still feasible here.

## Handling the hard orthogonal branches

The two hard top-level branches have the same orders as GAP's standard
orthogonal groups:

```gap
Order(GO( 1,8,2)) = 348364800;
Order(GO(-1,8,2)) = 394813440;
```

Both have index-2 derived subgroups:

```text
branch 8 derived subgroup order=174182400 index=2
branch 9 derived subgroup order=197406720 index=2
```

GAP's `tomlib` package has tables of marks for these derived orthogonal groups:

```text
O8+(2): 11171 table-of-marks classes
O8-(2): 5351 table-of-marks classes
```

The `atlasrep` package also has generating-set data for `S8(2)`, `O8+(2)`,
`O8-(2)`, and `O8-(2).2`. It does not have an `Sp8(2)` name in the tested
lookup, but `S8(2)` is available.

Recommended handling:

1. First try long isolated workers on the inherited representatives, because
   this preserves the embedding in `P` directly.
2. If those workers stall, switch to an index-2 lifting strategy. For
   `H = GO(epsilon,8,2)` let `N := DerivedSubgroup(H)`. Classify subgroups
   contained in `N` using the `O8+(2)` or `O8-(2)` table of marks as a guide,
   and classify subgroups not contained in `N` by lifting from
   `K cap N = L` through `N_H(L)/L`.
3. Use standard `GO(1,8,2)` and `GO(-1,8,2)` groups only as recognition and
   transport aids: build explicit isomorphisms to the corresponding embedded
   representatives inside `P`, then pull any standard-side subgroup data into
   the embedded branch.
4. Pass all pulled-back or lifted subgroups through the same global merge
   process. The merge step remains the source of truth for top-level
   `Sp(8,2)`-conjugacy.
5. Validate by checking orders, containment in the embedded branch, and
   top-level `P`-conjugacy against any representatives already discovered by
   other branches.

This keeps the main algorithm unchanged while giving the two largest
orthogonal branches a representation-specific escape hatch. These two branches
are the main remaining risk in the plan; the quick probes verify recognition
and library support, not completion of their full subgroup classification.

## Implementation readiness gates

Do not start a full unbounded run immediately after writing the scripts. Use
these gates:

1. `sp8_preflight.sh` must pass and write a run manifest with
   `RAM_WORK_GB = 48`, ordinary workers `<= 24`, heavy workers `<= 1`, and all
   GAP worker invocations using `-K`.
2. The `Sp(6,2)` validation run must reproduce 1369 classes through the same
   file-backed worker, merge, and recovery machinery intended for `Sp(8,2)`.
3. A first `Sp(8,2)` dry run should process only the top group and the light
   top-level maximal branches `1-7,10,11`, leaving branches 8 and 9 queued as
   heavy. The run should finish with no duplicate representative ids, no
   dangling raw child records, and no scheduler overcommit.
4. Kill and resume the dry run deliberately after at least one worker batch.
   Recovery must rebuild the same frontier and must not duplicate committed
   representatives.
5. Repeat the dry-run resume once after a completed worker batch but before
   merge. Recovery must reuse the raw child file and must not rerun the
   `MaximalSubgroupClassReps` subprocess for that representative.
6. Repeat the dry-run resume once after a completed merge proposal but before
   coordinator commit. Recovery must reuse the merge proposal or explicitly
   discard it without corrupting committed snapshots.
7. Only after the dry run passes should the scheduler expand deeper frontiers
   with 24 ordinary workers.
8. Branches 8 and 9 should remain isolated heavy jobs until one of them
   completes under cap, fails under cap and is requeued, or is handled by the
   table-of-marks/index-2 lifting fallback.

During the full run, the coordinator should abort new launches, not running
workers, if any of these conditions occur:

- available RAM drops below 8 GiB;
- swap use becomes nonzero and continues increasing;
- unmerged raw child records exceed the backpressure threshold;
- a merge bucket exceeds its memory cap;
- a worker exits from GAP break-loop behavior instead of clean success,
  timeout, or `-K` failure;
- the representative database fails its snapshot consistency check.

These abort conditions are meant to keep the computation restartable. They are
not mathematical failures; they are signals to merge, lower concurrency, or
promote jobs to a heavier class.

## Completion criteria

The computation should be considered complete only when all of the following
hold:

- the frontier queue is empty;
- every representative record is marked processed, except the trivial group if
  it has no proper maximal subgroups to process;
- every processed representative has a logged count of
  `MaximalSubgroupClassReps(H)`;
- every completed expensive job has an output artifact, success sentinel, and
  resource log;
- every failed, timed-out, or workspace-limited job is either requeued in a
  heavier class or marked `needs_special_handling`;
- every successful raw child file has either been merged or is attached to a
  pending merge bucket;
- every merge bucket has either been committed, is pending, or has a recorded
  failure reason;
- rerunning the closure check finds no maximal subgroup outside the database up
  to `P`-conjugacy;
- the final export can pull each representative back through `phi` to a matrix
  subgroup with matching order.

At that point the result is a certified list of `Sp(8,2)`-conjugacy classes of
subgroups, subject to GAP's correctness for the individual
`MaximalSubgroupClassReps` and `IsConjugate` calls.
