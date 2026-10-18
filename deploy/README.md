# Deploying ShelfShift to Google Cloud: the runbook

You run every command here yourself, in your own terminal (OPS-08). The assistant never runs a cloud command; it
only runs the scripts with `--dry-run` on a synthetic configuration. This runbook names configuration keys, never
their values: every name written with a leading `$` below is a key of `deploy/.env.example`, read from
your filled copy after

```
eval "$(.venv/bin/python -m deploy.envfile export)"
```

in the shell you run the step in (from the repository root). What was actually deployed is recorded by the scripts in
`deploy/releases/`; read it there. This runbook never repeats a recorded value (D-10).

## Before you start

Expected durations (06-REVIEWS R1-4): creating the Cloud SQL instance takes 5-10 min; each Cloud Build takes
3-8 min; the template build job and each bench job take a few minutes. Steps 1 to 6, the release and its read-back
checks take about two hours at the keyboard. Keep the laptop awake during a release; if it sleeps, step 7 says how to
resume.

### 1. Configuration and login

```
cp deploy/.env.example deploy/.env.local
```

Fill every `<placeholder>` in the copy (it is git-ignored; never commit it, never paste its values anywhere). Then:

```
gcloud auth login
gcloud auth application-default login
.venv/bin/python -m deploy.envfile check
```

The check prints `ok: 24 keys`, or one `config error:` line per problem, naming the key and line, never the value.
A copy made before `DATA_BUCKET` and `RELEASE_DATA_DIR` existed is refused until you add their two lines from
`deploy/.env.example`: fill `DATA_BUCKET`, and leave `RELEASE_DATA_DIR` empty for now.

### 2. Base setup

Review the commands first, then run them:

```
bash deploy/setup.sh base --dry-run
bash deploy/setup.sh base
```

It enables the APIs, creates the image repository and the run and deploy service accounts, and grants their project
roles. It is safe to re-run. If a role binding fails right after its account was created, wait a minute and re-run.

If the first Cloud Build (the early bench in step 4, or the first release) fails with a permission error writing the
image or its logs, Cloud Build is running as the project's Compute Engine default account, which this project has not
granted those rights (research Pitfall 8). Grant them, then rerun the step that failed:

```
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location "$REGION" --project "$PROJECT" \
  --member "serviceAccount:$(gcloud iam service-accounts list --project "$PROJECT" --filter='email~-compute@' --format='value(email)')" \
  --role roles/artifactregistry.writer
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$(gcloud iam service-accounts list --project "$PROJECT" --filter='email~-compute@' --format='value(email)')" \
  --role roles/logging.logWriter --condition None
```

### 3. The model metadata check

Two free, SDK-free requests (D-14, D-22): the model's catalog entry, then a token count on the `VERTEX_LOCATION`
endpoint (countTokens is free and generates nothing). They print the HTTP status and the response with the project ID
masked, never the access token. Keep the braces: zsh reads a colon right after an unbraced variable as a modifier on it
(`:c` here) and sends a broken URL.

```
curl -sS -o /dev/null -w 'catalog HTTP %{http_code}\n' -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: ${PROJECT}" "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/${GEMINI_MODEL}"
curl -sS -w '\nHTTP %{http_code}\n' -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H 'Content-Type: application/json' -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}' \
  "https://$([ "${VERTEX_LOCATION}" = global ] && echo aiplatform.googleapis.com || echo "${VERTEX_LOCATION}-aiplatform.googleapis.com")/v1/projects/${PROJECT}/locations/${VERTEX_LOCATION}/publishers/google/models/${GEMINI_MODEL}:countTokens" \
  | sed "s/${PROJECT}/<PROJECT>/g" | tail -c 300
```

Reading it:

- `catalog HTTP 200` and the count's `HTTP 200`: `model=ok`. Keep `VERTEX_LOCATION`.
- `catalog HTTP 404`: the model ID in `GEMINI_MODEL` is wrong; fix it (`model=blocked:model-id`).
- The count's `HTTP 404` or `400` with a JSON error: run the count once more on the global endpoint
  (`export VERTEX_LOCATION=global`, then the second command again). A `200` there means the model is not served on
  your location: set `VERTEX_LOCATION=global` in your configuration (`model=global`).
- An HTML "Error 404 (Not Found)" page means the URL itself is broken (most often a lost brace), not the model: fix
  the command and rerun.
- `HTTP 401` or `403` (not logged in, a missing role, the API not enabled), or a curl or gcloud error: this is
  `model=blocked:<reason>`. Fix the cause and rerun; never change the endpoint for it.

This is catalog evidence and a token count only: it proves neither generation, billing access, tool calling nor the
runtime identity (Phase 9's live tests do); pay-as-you-go generation may be offered on fewer locations than the count. Gemini makes no other call in Phase 6. The token goes into the request header only and
expires within the hour.

### 4. The early bench

```
bash deploy/bench-early.sh
```

Before the database exists, it builds (or reuses) the release image from the committed tree and times the engine on
Cloud Run with the frozen input (D-15). It prints the in-process startup time, the three measured runs and their
median, and writes `deploy/releases/<UTC>-<sha12>-early-bench.json`; commit that file. A median at or above 3 s stops
the deployment for investigation. The case shortlist, set in the reference data, is the only lever; the 3 s budget is
never raised. The release bench (step 7) stays the gate.

### 5. The cost estimate

Before `setup data` creates the instance (D-02), price the whole judging window: min 1 instance from submission until
2026-11-06 (D-01), the Cloud SQL instance, storage and logging, and the Gemini use expected from Phase 9. If a month's
estimate exceeds the alert amount you set in `BUDGET_AMOUNT_USD`, raise `BUDGET_AMOUNT_USD` to the estimate plus a
margin. An alert only notifies; it never stops spending. The amount stays in your configuration and never goes into a
public file.

### 6. Data setup and the grants

```
bash deploy/setup.sh data --dry-run
bash deploy/setup.sh data
```

It creates the Cloud SQL instance (PostgreSQL 17, IAM login on, the default public IP), the database, both IAM
database users, the passcode secret, the secret's one reader and the budget alert. It generates the reviewers'
passcode itself, at least 8 random characters, sends it to Secret Manager and never prints it (D-24). An existing
secret is kept; pass `--new-passcode` to replace it with a new version, which the next release then pins.

To read the passcode when you need it, run this in a terminal of your own, never through an assistant session (a
session keeps everything shown in it):

```
gcloud secrets versions access latest --secret "$PASSCODE_SECRET" --project "$PROJECT"
```

Then, once and before the first release, apply the two schema grants that `setup data` printed, as postgres:

```
gcloud sql users set-password postgres --instance "$SQL_INSTANCE" --prompt-for-password --project "$PROJECT"
gcloud sql connect "$SQL_INSTANCE" --user postgres --database "$DB_NAME" --project "$PROJECT"
```

and paste the printed GRANT lines at the postgres prompt (every build job grants the runtime user its tables and
sequences itself). `gcloud sql connect` runs the Cloud SQL Proxy, so install it once with
`gcloud components install cloud-sql-proxy`; it reaches the instance through its public IP, which `setup data` keeps
(the Cloud SQL connector uses it too), so a private-only instance will not connect (R1-4). Each pasted line answers
`GRANT`; the prompt shows the database name, and a line ending without `;` leaves an unfinished statement (`->`).

#### The HQ secret

The HQ planner signs in with a credential of its own (06.1 D-47), kept in the Secret Manager secret that
`HQ_PASSCODE_SECRET` names. You create it; no script does. `bash deploy/setup.sh data` grants the run account read
access to it and, when it does not exist yet, says so in its closing lines; it never creates, rotates or reads it.

Run this in your own Terminal (zsh), not through an assistant session, because the hidden prompt needs a real
terminal. The value is typed hidden, goes to gcloud on standard input only, is compared with the store passcode in
memory and is cleared at once; nothing prints it:

```zsh
eval "$(.venv/bin/python -m deploy.envfile export)"
read -rs "hq_value?HQ password (hidden, at least 12 characters): "; echo
if [ ${#hq_value} -lt 12 ]; then echo "Too short. Nothing was created."; unset hq_value
elif [ "$hq_value" = "$(gcloud secrets versions access latest --secret "${PASSCODE_SECRET}" --project "${PROJECT}")" ]; then echo "Same as the store passcode. Nothing was created."; unset hq_value
else printf %s "$hq_value" | gcloud secrets create "${HQ_PASSCODE_SECRET}" --project "${PROJECT}" --replication-policy automatic --data-file=- && echo "Secret created."; unset hq_value
fi
gcloud secrets add-iam-policy-binding "${HQ_PASSCODE_SECRET}" --project "${PROJECT}" --member "serviceAccount:${RUN_SA_EMAIL}" --role roles/secretmanager.secretAccessor --condition None --format none && echo "Run account can read the HQ secret."
gcloud secrets versions list "${HQ_PASSCODE_SECRET}" --project "${PROJECT}" --filter "state=ENABLED" --format "value(name)"
```

The last command prints only the enabled version numbers (for example `1`), never the value. `hq_value` is a shell
variable of this block only, not a configuration key. `HQ_PASSCODE_SECRET`
must name another secret than `PASSCODE_SECRET`; every command refuses a configuration where they are the same.

To change it later, run the same `read` line, then
`printf %s "$hq_value" | gcloud secrets versions add "${HQ_PASSCODE_SECRET}" --project "${PROJECT}" --data-file=-; unset hq_value`.
The next release pins the new version.

When `setup data` ends with `setup data: budget=manual` (R1-2: it could not create the budget, most often for want of
a billing-account role), create the budget in the console exactly as it printed: Billing, Budgets & alerts, the name
from `BUDGET_NAME`, `BUDGET_AMOUNT_USD` a month on this project only, alerts at 50%, 90% and 100%. Step 8 lists it.
A budget lives on the billing account the project is actually billed to and is in that account's currency.
`setup data` says so when `BILLING_ACCOUNT` is not the project's account (set the key to the account
`gcloud billing projects describe "$PROJECT"` names, then rerun) and when the account does not bill in USD (enter the
equivalent of `BUDGET_AMOUNT_USD` in that currency, in the console or with `--budget-amount <amount><currency>`).

`setup data` also creates the private bucket named in `DATA_BUCKET` (in `REGION`, uniform bucket-level access, public
access prevention on) and lets the deploy account read it, on that bucket only. If you ran this step before, run
`bash deploy/setup.sh data` once more: what exists is left alone. A bucket name is global: if the create says the name
is taken, choose another `DATA_BUCKET`.

### 7. The release

```
bash deploy/release.sh --dry-run
bash deploy/release.sh
```

In order, it refuses an uncommitted tree, a failing reference check, a failing deploy gate or a browser that cannot
start; opens its record in `deploy/releases/`; pins both secrets and runs the HQ preflight (below); builds the image with Cloud Build from the committed source (or reuses
it) and deploys everything by digest; builds the template (the build job); benches it (the bench job, slowest of 3
under 3 s); deploys the service at the service level with min instances from `MIN_INSTANCES` and max 1; reads the
deployment back; checks public access; and runs the smoke and the deployed journey. A release is known good only
after all of that passed (D-09). Before anything changes, the release records the revision serving the service's
traffic and, from the build's `previous ACTIVE:` line, the template ACTIVE before the build: the pair a failed release
goes back to. From the moment the build job is executed, every failed or uncertain outcome settles that build
execution, restores that template (the restore's own `ACTIVE:` line must name it) and moves traffic back to that
revision (read back as the only one serving) (D-17).

- **The record.** Each run writes `deploy/releases/<UTC>-<sha12>.json`. Read what was deployed there; commit it
  before the next release.
- **The secret step comes first (06.1 D-64).** Before any image, build, job or deploy command, the release pins the
  latest enabled version of `PASSCODE_SECRET` and of `HQ_PASSCODE_SECRET` (never `latest`; the record keeps both
  numbers, `secret_version` and `hq_secret_version`, never a value), then reads both pinned versions into memory and
  refuses when the store value is empty, or the HQ value is empty, shorter than 12 characters or the same as the store
  passcode. The refusal is `failed_secret`, names the key and the rule, never a value, and nothing was built, so the
  serving revision keeps serving. Fix it with step 6, The HQ secret (add a new version), then release again. The
  product app's own startup check repeats the rule, so a bad mount never takes traffic either. A rollback and a
  reconcile deploy nothing new, so they run no preflight.
- **Product mode (06.1 D-62).** The service runs with `DEMO_MODE` off and mounts both pinned versions:
  `DEMO_PASSCODE` (the store passcode) and `HQ_PASSCODE`. The demo endpoints answer 403, and the removed ones 404.
- **The checks.** The smoke signs in as HQ and as the story's source store, finds the story by its tag, proves a store's
  policy edit is refused (403) and HQ reaches the policy write path (an out-of-range value refused with 400, nothing
  written), proves the demo endpoints are refused and the store passcode does not open HQ, then runs the what-if,
  the export and the passcode counter. The deployed journey signs in as the HQ planner in the browser, signs out and
  runs the store journey from sign-in to Close. Both credentials reach them in memory or in the child's environment
  only (`SMOKE_PASSCODE` and `SMOKE_HQ_PASSCODE`), never printed. Each release opens two sandboxes of the daily cap,
  both its own (the smoke's and the journey's); judges' sandboxes are never touched.
- **The image.** A missing image tag is built. Any other failure of the tag's describe (signed out, permission
  denied, no gcloud) stops the release at the image step and builds nothing; its recovery says `not needed` (A3).
- **Status, any time.** `bash deploy/release.sh --status` prints the service's traffic, the serving revision, the
  ACTIVE template and the newest records, writes nothing, and refuses while a release runs; step 11 shows how to read
  it.
- **One at a time.** One release or rollback runs at a time from this checkout (the release lock, R2-11). After a
  crash or a closed laptop, run `bash deploy/release.sh` again: it reconciles the interrupted release (settles its
  build, restores, moves traffic back) and stops. Commit that record, then run it once more.
- **Traffic.** When the service's traffic is split between revisions, or cannot be read, the release refuses before
  it builds anything: send 100% to one revision first. A service that exists but has never had a ready revision (its
  first deploy never became ready) counts as a first release, because nothing serves (WR-07). After each deploy the
  release sends 100% to the new revision itself (`--to-latest`), because a recovery pins traffic and Cloud Run then
  stops routing new deploys.
- **An unfinished recovery.** Until a recovery has finished, its record says `reconcile: pending`. The next
  `bash deploy/release.sh` retries that release's recovery first and starts no build; commit the record, then run it
  again. When the restore puts back a template other than the one recorded, the release stays blocked on purpose: the
  record names the template that is ACTIVE and the one expected; find out how it became ACTIVE before releasing again.
  - Settle never concludes `never started` while a build-job execution it cannot match to this release still runs:
    the record stays pending until that execution ends (at most the build job's 180-minute task timeout), then rerun (WR-08).
  - When settle cannot read the build after the service deploy, traffic still moves back (read back) and only the
    restore waits for the rerun (WR-11).
  - A traffic move whose command exits non-zero still counts when the read-back shows that revision alone at 100%;
    the run then prints one line naming the exit code. Otherwise it is failed as before (A5).
- **A first release on a fresh database.** A first release that fails before its build log is read (for example,
  the step-6 grants were not applied, so the build fails at migrate) goes back to no template ACTIVE:
  `recovery: restore: nothing to do: none ACTIVE`, or `undone: none ACTIVE` when its template had become ACTIVE. It
  is not left pending, so the next run builds. This holds only while there is no earlier activated template, no
  known-good release and no revision serving (WR-06); otherwise `none ACTIVE` is a mismatch and the record stays
  pending.
- **Access (R1-1).** The release reads the service's IAM policy back. When an organisation policy refused the public
  `allUsers` binding, the release turns off the invoker check itself with `--no-invoker-iam-check` (the passcode
  gate is the access control either way). When that is refused too, the release fails at access and restores. A
  deploy that exits non-zero goes on to these checks only when its revision is the one this deploy created (WR-09).
  The fix is an organisation-policy exception on the project, for domain-restricted sharing
  (`iam.allowedPolicyMemberDomains`) or for the invoker-check constraint, granted by the organisation's
  administrator. Never ship a private URL.
- **The public dataset (release day).**
  1. Set `RELEASE_DATA_DIR` in `deploy/.env.local` to the folder that holds the public set's `manifest.json` and
     `parts/` (outside this checkout; a relative path is read from the repository root).
  2. Run `bash deploy/release.sh --dry-run`, then `bash deploy/release.sh`.

  Before any cloud command, the release checks the folder; a refusal names the part. It uploads the listed parts,
  then `manifest.json` last, under the dataset version. A rerun uploads the same files again, unchanged, and unlisted
  files such as the forecast input are never uploaded. The build job reads that version from the bucket, read-only,
  and the release fails and recovers unless the build activated it. The bench then checks the 20 hardest cases,
  picked before timing, each slowest of 3 under 3 s, and 10 resets, each under 2 s. Leave `RELEASE_DATA_DIR` empty
  to build `data/reference` as before. If the build job cannot start because it cannot read the bucket, run
  `bash deploy/setup.sh data` again, then `bash deploy/release.sh` (it finishes the recovery first). Never delete the
  bucket while the build job mounts it.
- **Template compatibility (R2-10).** A template change the image now serving cannot read (the release says
  breaking: another `ENGINE_VERSION` than the last known-good release) is released with `--breaking-template` only
  while no judge is using the app: visitors who open a sandbox during that release would get a loud 409 until it
  finishes.

### 8. Read-back checks

The service URL is in the release record. Run these in your own terminal:

- **Cloud SQL (Day-1 check 6).** Expect `db-f1-micro`, `ENTERPRISE` and `cloudsql.iam_authentication=on`:

  ```
  gcloud sql instances describe "$SQL_INSTANCE" --project "$PROJECT" --format='value(settings.tier,settings.edition,settings.databaseFlags)'
  ```

  then, inside `gcloud sql connect "$SQL_INSTANCE" --user postgres --database "$DB_NAME" --project "$PROJECT"`,
  `SHOW max_connections;` should answer 25.
- **The service (check 8).** Service-level min equal to `MIN_INSTANCES` and max 1, CPU throttling on, startup boost
  on, the gen2 execution environment:

  ```
  gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format=json | grep -E 'minScale|maxScale|cpu-throttling|startup-cpu-boost|execution-environment'
  ```

  With `MIN_INSTANCES=0` Cloud Run returns no `minScale` at all, so the record says `not read back` for min; that is
  the default 0, never a guess. A missing `maxScale` fails the release (without it Cloud Run allows many instances),
  and so does a missing `minScale` while `MIN_INSTANCES` is 1.

- **Cold start (check 10).** After more than 15 idle minutes:
  `curl -s -o /dev/null -w '%{time_total}\n' <service URL>/api/healthz`. Record the time as measured; a fast or slow
  first answer does not by itself prove how many instances were running.
- **The SPA guard (check 11).** The smoke's `spa guard` line in the record, or by hand: an encoded `..` path must
  answer 404 or the same page as `/`, never source code. First its status,
  `curl --path-as-is -s -o /dev/null -w '%{http_code}\n' '<service URL>/..%2f..%2fapi/config.py'`; a `404` passes.
  A `200` passes only when its body is the page `/` serves:
  `curl --path-as-is -s '<service URL>/..%2f..%2fapi/config.py' | cmp - <(curl -s '<service URL>/') && echo same-as-index`.
- **The budget.** `gcloud billing budgets list --billing-account "$BILLING_ACCOUNT" --billing-project "$PROJECT" --format='value(displayName)' | grep -x "$BUDGET_NAME"`
  prints the budget's name once.
- **The passcode counter (D-24).** The smoke's `passcode counter` line in the record is ok: a guess with a forged
  X-Forwarded-For was counted under this machine's own address.
- **The second-network check (required; 06-REVIEWS R2-5, user decision D-R2).** One counter per visitor is what the
  smoke cannot prove from one machine. Just after a clock minute turns, send two wrong guesses from this network, then
  switch this laptop to another network (a phone hotspot, for example) and send one more, all within that same
  minute (if the minute turns, wait a minute and start again):

  ```
  curl -sS -X POST -H 'Content-Type: application/json' -d '{"passcode": "not-the-passcode"}' '<service URL>/api/session'
  ```

  Each answer shows `attempts_left`. When the guess from the other network restarts near 9, the networks are counted
  apart: answer `separate`. When it continues this machine's count, every visitor shares one counter: answer
  `shared`. `shared` or `not run` keeps D-24 unverified and Phase 6 open. No address is printed, and none is ever
  written down.
- **The journey, any time.** `make journey-deployed URL=<service URL>` re-runs the judge's path in a browser; it reads
  `SMOKE_PASSCODE` and `SMOKE_HQ_PASSCODE` or asks for both (hidden prompts), so run it in your own terminal.

### 9. Commit the record, run the push gate, push

```
git add deploy/releases/
git commit -m "release: record"
make deploy-check
```

`make deploy-check` is the push gate. It loads PROJECT from `deploy/.env.local` through `deploy/envfile.py`, scans
every tracked file (`deploy/releases/` excluded) for it and for the earlier project's ID, runs the deploy tests and
checks every deploy script's syntax. It fails when the file, the key or a real value is missing. Push the code only
when it passes: `git push` of the build/deploy branch, never the docs.

### 10. Rollback

```
bash deploy/release.sh --rollback
```

It restores the latest known-good release's previous template and writes a `-rollback.json` record in
`deploy/releases/`; commit it. Only when the restore's `ACTIVE:` line names that template does it send that release's
previous revision 100% of traffic (read back). A traffic move whose command exits non-zero still counts when the
read-back shows that revision alone at 100%; the rollback then prints one line naming the exit code. Otherwise it is
failed as before (A5). With no previous revision (the first release), or no previous template
in that release's record, it refuses. When the restore changes nothing (that release's template is no longer ACTIVE)
or puts another template back, it refuses: no traffic moves, the record says `failed_rollback`, and the refusal names
the `ACTIVE:` line it read. A pending release is reconciled by the rollback itself, which then exits 1: commit that
record and run the rollback again.

**A half-done rollback** (the restore was done, but the traffic move failed or did not read back): run
`bash deploy/release.sh --rollback` again. It sees `nothing to do: <previous template> ACTIVE`, finds the earlier
rollback that restored it, moves the traffic, reads it back and records `rolled_back` (it prints
`rollback: resumes <id>`). Do not move the traffic by hand: the records would then still name the rolled-back release
as serving, and the next release's compatibility check would read the wrong engine version (WR-10).

### 11. Recovery drill (before a judges' release)

#### Why and where

A release that works proves only the happy path. The drill forces failures on a separate service with its own
database, so the undo, retry and rollback paths (06-REVIEW CR-01, CR-02 and WR-06 to WR-11) run on real Cloud Run
without touching the judges' service.

- **Shared with live:** `PROJECT`, `REGION`, the image repository, the Cloud SQL instance, both service accounts and
  the passcode secret. **Its own:** the service, the three jobs, the database and the records directory
  (`deploy/releases-drill/`, git-ignored).
- Run every command from the root of the build/deploy checkout (the `ShelfShift-deploy` folder), on the same clean
  commit you will release, and outside judging use: the drill shares the db-f1-micro instance.
- Expect about two and a half hours (three with the optional D4 to D6). Each run repeats the local checks, the build
  job and the bench job. The first drill release builds the image for this commit; later runs (and the live release)
  reuse it. Each `--status` runs the build job once (about a minute).
- Every drill command carries `--env-file deploy/.env.drill`. Without it the command acts on the live app: a plain
  release is then a real live release, and the drill switches are refused. A drill run's first line says
  `drill configuration: records go to deploy/releases-drill/ (git-ignored)`.

#### Setup

1. **The drill configuration**, built without opening the live file:

   ```
   sed -E -e 's/^(SERVICE|BUILD_JOB|BENCH_JOB|EARLY_BENCH_JOB)=(.+)$/\1=\2-drill/' -e 's/^DB_NAME=(.+)$/DB_NAME=\1_drill/' -e 's/^MIN_INSTANCES=.*$/MIN_INSTANCES=0/' deploy/.env.local > deploy/.env.drill
   .venv/bin/python -m deploy.envfile check --env-file deploy/.env.drill
   bash deploy/release.sh --dry-run --env-file deploy/.env.drill
   ```

   The check prints `ok: 24 keys`. The sed copies `DATA_BUCKET` and `RELEASE_DATA_DIR` from the live file
   unchanged: the drill shares the bucket (its files sit under the dataset version and every job only reads them).
   The dry run prints the drill line and ends with `dry run: complete`; a
   `config error:` naming keys means the five names are not all marked. Both new files are git-ignored; never paste
   either anywhere. The check refuses a name the suffix made too long (more than 50 characters for the service and job
   names, more than 63 for `DB_NAME`): shorten only that value of `deploy/.env.drill` in your own editor, keeping the
   `-drill` (or `_drill`) ending, and check again. If gcloud refuses a drill name as too long at the first drill
   release, shorten it the same way.

2. **The drill database and its grants.**

   ```
   bash deploy/setup.sh data --env-file deploy/.env.drill | tee /dev/stderr | grep '^GRANT ' > /tmp/shelfshift-drill-grants.sql
   wc -l < /tmp/shelfshift-drill-grants.sql
   ```

   On the existing instance it creates only the drill database; the users, the secret and the budget exist and are
   left as they are, and its last line is `setup data: budget=…`, as for live. `wc` prints 2. Do not apply the
   grants yet: run D0 first.

   **D0, a first release before the grants:** `bash deploy/release.sh --env-file deploy/.env.drill`. Its build fails
   at migrate, because the drill database has no grants yet.
   - Run: the first `+ gcloud artifacts docker images describe` line is followed by `+ gcloud builds submit` (the
     tool read gcloud's real not-found answer for a new tag), then
     `release failed at build_execute (failed_build): …`.
   - If it stops at the image step instead, with
     `release failed at image (failed_image): the image tag … could not be read, so nothing was built: …`: stop the
     drill and report the words after `(gcloud.artifacts.docker.images.describe)`, with any image path written as
     `<image>`. Nothing was built, and its recovery says `not needed`.
   - `recovery: settle:` with the state `finished`. If it says `never started`, the tool did not recognise the real
     executions-list shape: report those words.
   - `recovery: restore: nothing to do: none ACTIVE`, no `recovery: traffic` line and no `reconcile: pending` line.
   - Status (`bash deploy/release.sh --status --env-file deploy/.env.drill`): `service: not deployed yet`,
     `ACTIVE: none`, `newest record: <D0> failed_build`, `known good: none`.

   Then apply the grants:

   ```
   eval "$(.venv/bin/python -m deploy.envfile export --env-file deploy/.env.drill)"
   gcloud sql connect "${SQL_INSTANCE}" --user postgres --database "${DB_NAME}" --project "${PROJECT}"
   ```

   The proxy and the postgres password are as in step 6 (if you no longer know the password, step 6's
   `set-password` command sets a new one). At the prompt, which shows the drill database's name, type
   `\i /tmp/shelfshift-drill-grants.sql`: two `GRANT` answers. Then `\q` and `rm /tmp/shelfshift-drill-grants.sql`.
   This terminal now holds the drill's values for `$` commands: use another terminal for live gcloud commands.
   `bash deploy/release.sh` reads its file, never these exports.

3. **The baseline:** `bash deploy/release.sh --status --env-file deploy/.env.drill`. Expect D0's status lines
   again: the grants change no template, so `ACTIVE: none` still holds.

#### The sequence DL to DW

Name each drill run by its record id (the run prints it) and call it by its step name in your notes (DL, DU, D1 and
so on). After each step, run `bash deploy/release.sh --status --env-file deploy/.env.drill` and compare. The
`[release …]` labels on the `traffic:`, `latest ready:` and `serving:` lines say which release deployed each revision.

- **DL, a first build whose log is never read:**
  `bash deploy/release.sh --env-file deploy/.env.drill --drill-fail-after build_execute`. The build now works (the
  grants are applied) and activates its template; the run stops right after the build execute, before it reads the
  build log (WR-06 path 2).
  - Run: `release failed at build_execute (failed_build): drill: forced failure after build_execute`,
    `recovery: settle:` with the state `finished`, `recovery: restore: undone: none ACTIVE` (the restore ran with
    `--first`), no `recovery: traffic` line and no `reconcile: pending` line.
  - Status: `service: not deployed yet`, `ACTIVE: none`, `newest record: <DL> failed_build`, `known good: none`.
- **DU, a first deploy that never becomes ready:**
  `bash deploy/release.sh --env-file deploy/.env.drill --drill-unready-deploy`. Its service deploy carries a value the app refuses at start, so the revision never becomes ready (WR-07). The
  deploy may take a few minutes before gcloud gives up.
  - Run: `release failed at service_deploy (failed_deploy): gcloud run deploy failed (exit 1)` (report it only up to
    `(exit 1)`), `recovery: settle:` with the state `finished`,
    `recovery: restore: not run: no template was ACTIVE before this build, so there is none to go back to`,
    `recovery: traffic: not moved: no previous revision (the first release)`, and no `reconcile: pending` line.
  - DU's template stays ACTIVE: its build saw no template ACTIVE before it, so there is none to go back to (accepted
    behaviour). D1's build retires it.
  - Status: `traffic: 100% to (unnamed) [not in the records]` or no `traffic:` line, `latest ready: none`,
    `serving: none (no revision is ready; the next release is a first release)`, `ACTIVE:` a template id (DU's),
    `newest record: <DU> failed_deploy`, `known good: none`.
  - If the status says `service: not deployed yet` instead, report it: Cloud Run kept no service, and D1 is then a
    first release because there is no service.
- **D1, the first release:** `bash deploy/release.sh --env-file deploy/.env.drill`.
  - Run: `release <D1>: known good, …`. It does not fail at `previous_revision`: a service with no ready revision is
    released as a first release (WR-07).
  - Status: `serving: … [release D1]`, `ACTIVE:` D1's template, both `yes` checks, and the previous revision
    `none (the first release)`.
- **D2, the real undo:** `bash deploy/release.sh --env-file deploy/.env.drill --drill-fail-after smoke`.
  - Run: `release failed at smoke (failed_smoke): drill: forced failure after smoke`,
    `recovery: restore: restored: <D1's template> ACTIVE`, `recovery: traffic: <D1's revision>=100`, and no
    `reconcile: pending` line.
  - Status: `latest ready: … [release D2]`, `serving: … [release D1]`, both checks `yes`.
  - If the latest ready and serving revisions have the same name (Cloud Run made no new revision for the same image
    and settings), the traffic half was not exercised: report `no new revision`.
- **D3, routing after pinned traffic:** `bash deploy/release.sh --env-file deploy/.env.drill`.
  - Run: `release <D3>: known good, …` (`--to-latest` moved the pinned traffic to the new revision).
  - Status: one `traffic:` line at 100% `[release D3]`, `serving: … [release D3]`, both checks `yes`, and the
    previous revision `[release D1]`, never D2.
- **DK, a stop during the build, then the rerun's wait (WR-08):** paste the command below as one line. It is a single
  `bash -c` line on purpose: when a foreground job dies of Ctrl-C, zsh drops the rest of a pasted block, so the
  rerun would never run. Inside it, the release runs in the foreground; about 20 seconds after the build execute
  starts, `pkill` sends Ctrl-C to that drill release only (it matches `deploy.release --env-file deploy/.env.drill`,
  which a live release never carries); then the same release runs again and the last line counts how often it polled
  the running build.

```
bash -c 'log=$(mktemp); (until grep -q "api\.sandbox,build,--run-id" "$log"; do sleep 1; done; sleep 20; pkill -INT -f "deploy\.release --env-file deploy/\.env\.drill") & waiter=$!; bash deploy/release.sh --env-file deploy/.env.drill 2>&1 | tee "$log"; kill "$waiter" 2>/dev/null; if grep -q KeyboardInterrupt "$log"; then bash deploy/release.sh --env-file deploy/.env.drill 2>&1 | tee "$log.rerun"; printf "describe polls: %s\n" "$(grep -c "executions describe" "$log.rerun")"; else printf "not stopped during the build: no rerun\n"; fi; rm -f "$log" "$log.rerun"'
```

  - First run: up to the `+ gcloud run jobs execute …api.sandbox,build,--run-id,…` line, then about 20 seconds later
    a Python traceback ending in `KeyboardInterrupt`.
  - Rerun: `reconcile: the release record <DK> is still in_progress …`, `+ gcloud run jobs executions list` lines,
    then `+ gcloud run jobs executions describe` lines about 10 seconds apart, `recovery: settle:` with the state
    `finished`, `recovery: restore: restored: <D3's template> ACTIVE` (or `nothing to do: <D3's template> ACTIVE` if
    the build failed), no `recovery: traffic` line, exit 1 and no build.
  - The last line is `describe polls: N`. N of 1 or more proves the wait while the build still runs (WR-08). N = 0
    means the build had ended before the rerun listed it: paste the command again with `sleep 20` changed to
    `sleep 5`.
  - Report instead: `recovery: settle: unknown: … still running …` (the real shape was not recognised; wait 10
    minutes and rerun), or `not stopped during the build: no rerun` with the last lines before it.
  - Status: `newest record: <DK> interrupted`, `serving: … [release D3]`, both checks `yes`.
- **DS, a settle that cannot be read (WR-11):**
  `bash deploy/release.sh --env-file deploy/.env.drill --drill-fail-after smoke --drill-fail-settle-once`, then the
  plain `bash deploy/release.sh --env-file deploy/.env.drill`.
  - First run: `release failed at smoke (failed_smoke): drill: forced failure after smoke`,
    `recovery: settle: unknown: drill, not run`,
    `recovery: restore: not run: a build that may still be running could promote after it`,
    `recovery: traffic: <D3's revision>=100`, and the `reconcile: pending` line.
  - Status: `newest record: <DS> failed_smoke, reconcile: pending`, the serving check `yes`, the ACTIVE check `no`
    (DS's template is still ACTIVE, because the restore waited).
  - Rerun: `reconcile: the release record <DS> is marked reconcile: pending; …`, `recovery: settle:` with the state
    `finished`, `recovery: restore: restored: <D3's template> ACTIVE`, `recovery: traffic: <D3's revision>=100`. It
    exits 1 and builds nothing, by design. Drill records are git-ignored: commit nothing.
  - Status: `newest record: <DS> interrupted`, both checks `yes`.
- **D4 to D6 (optional, proved twice on 2026-10-17).**
  - **D4, a failed undo retried first:**
    `bash deploy/release.sh --env-file deploy/.env.drill --drill-fail-after smoke --drill-fail-restore-once`, then
    the plain `bash deploy/release.sh --env-file deploy/.env.drill`. First run: `recovery: restore: failed: drill,
    not run`, `recovery: traffic: <D3's revision>=100`, and the `reconcile: pending` line; the status shows
    `newest record: <D4> failed_smoke, reconcile: pending`, the serving check `yes`, the ACTIVE check `no`. Rerun:
    `recovery: restore: restored: <D3's template> ACTIVE`, `recovery: traffic: <D3's revision>=100`, exit 1, no
    build; the status shows `newest record: <D4> interrupted`, both checks `yes`.
  - **D5 and D6, two failed releases in a row:**
    `bash deploy/release.sh --env-file deploy/.env.drill --drill-fail-after smoke`, twice. Each recovery restores
    D3's template and sends traffic to D3's revision, never D5's. Status after D6: `latest ready: … [release D6]`,
    `serving: … [release D3]`, both checks `yes`.
- **DR, a rollback whose traffic move fails once, resumed (WR-10):**
  `bash deploy/release.sh --rollback --env-file deploy/.env.drill --drill-fail-rollback-traffic-once`, then the
  plain `bash deploy/release.sh --rollback --env-file deploy/.env.drill`.
  - First run: `rollback: restore: restored: <D1's template> ACTIVE` (D3's predecessor),
    `rollback: traffic: failed: drill, not run`, `rollback failed_rollback; … commit nothing`, exit 1, and no
    `+ gcloud run services update-traffic` line.
  - Status: `newest rollback: <DR1> failed_rollback`, `known good: <D3> …`, `serving: … [release D3]`, the serving
    check `yes`, the ACTIVE check `no` (D1's template is ACTIVE while D3 still serves).
  - Second run: `rollback: resumes <DR1>: its restore put <D1's template> back; only the traffic move was left`,
    `rollback: restore: nothing to do: <D1's template> ACTIVE`, `rollback: traffic: <D1's revision>=100`,
    `rollback rolled_back; …`.
  - Status: `newest rollback: <DR2> rolled_back`, `known good: <D1> …`, `serving: … [release D1]`, both checks
    `yes`.
  - Or step 10's documented refusal (`rollback refused: …`, no traffic moved): report that line.
- **D7 (optional), a rollback already done:** the plain `bash deploy/release.sh --rollback --env-file deploy/.env.drill`
  again. Run: `rollback: <D3> was already rolled back by <DR2>; nothing to do`, exit 0, and no `+ gcloud` line. The
  status is unchanged.
- **DW, an unready deploy after a failed release (WR-09), the last step:**
  `bash deploy/release.sh --env-file deploy/.env.drill --drill-unready-deploy`. It runs last because it leaves a
  revision that never became ready as the service's latest created one. Before it, the latest ready revision is
  DS's (or D6's), a failed release of the same image, while D1's serves.
  - Run (as seen on 2026-10-17): the traffic is pinned to one revision, so `+ gcloud run deploy` exits 0 although
    its new revision never becomes ready, and the next line, `+ gcloud run services update-traffic … --to-latest`,
    fails: `release failed at service_deploy (failed_deploy): gcloud run services update-traffic failed (exit 1)`
    (report it only up to `(exit 1)`). The deploy fallback is not reached, and nothing is routed to the new
    revision or to the failed release's ready one. Then `recovery: settle:` with the state `finished`,
    `recovery: restore: restored: <D1's template> ACTIVE`,
    `traffic: the move to <D1's revision> exited 1, but the read-back shows <D1's revision>=100: counted as moved`
    (update-traffic exits 1 while a revision that never became ready is the latest created one; A5),
    `recovery: traffic: <D1's revision>=100`, and no `reconcile: pending` line.
  - If instead `release failed at service_deploy (failed_deploy): gcloud run deploy failed (exit 1)` appears, the
    deploy fallback ran: report it with the lines after it.
  - If instead `recovery: traffic: failed: …` appears with the `reconcile: pending` line, report both and go to the
    cleanup without rerunning.
  - Status: `latest ready: … [release DS]` (or D6's when you ran D4 to D6), `serving: … [release D1]`,
    `newest record: <DW> failed_deploy`, `known good: <D1> …`, both checks `yes`.

#### What to report after each step

- the run's final status line;
- its `recovery:` or `rollback:` lines, with each revision name replaced by its step label (the `[release …]` labels
  show which) and the settle result shortened to its state;
- for a failed deploy, its `release failed at service_deploy` line only up to `(exit 1)`: the rest can carry a
  revision name or a logs URL;
- DK's `describe polls:` line, and for DW whether the `traffic: the move to …` line appeared;
- the `--status` lines `newest record`, `newest rollback`, `known good`, the three `check:` lines and the bracketed
  labels of `traffic`, `latest ready` and `serving`.

Never paste a URL, project ID, passcode, service, job or revision name.

#### Cleanup, after reporting

```
eval "$(.venv/bin/python -m deploy.envfile export --env-file deploy/.env.drill)"
[[ "${SERVICE}" == *-drill ]] && gcloud run services delete "${SERVICE}" --region "${REGION}" --project "${PROJECT}"
[[ "${BUILD_JOB}" == *-drill ]] && gcloud run jobs delete "${BUILD_JOB}" --region "${REGION}" --project "${PROJECT}"
[[ "${BENCH_JOB}" == *-drill ]] && gcloud run jobs delete "${BENCH_JOB}" --region "${REGION}" --project "${PROJECT}"
[[ "${EARLY_BENCH_JOB}" == *-drill ]] && gcloud run jobs delete "${EARLY_BENCH_JOB}" --region "${REGION}" --project "${PROJECT}"
[[ "${DB_NAME}" == *_drill ]] && gcloud sql databases delete "${DB_NAME}" --instance "${SQL_INSTANCE}" --project "${PROJECT}"
rm deploy/.env.drill
rm -r deploy/releases-drill
```

Each delete is guarded: a live name never matches the pattern, so a shell still holding live values deletes nothing.
gcloud asks you to confirm each one. The early bench job exists only if you ran the early bench on the drill, so a
not-found answer there is fine. Then close that terminal.

#### Then: the controlled live release

In a new terminal, at the same root:

1. `bash deploy/release.sh --status`, the before capture. Expect one `traffic:` line at 100%
   `[release <the latest live record>]`, `serving:` the same, `known good: <id> (template …)` and the serving check
   `yes`. First make sure gcloud is signed in (`gcloud auth list` names your account as active). Then
   `ACTIVE: unknown (the build job's execution failed, exit …: gcloud itself may have failed, …, or the build job is
   missing or its image predates api.sandbox active)` is expected while the live build job's
   image predates api.sandbox active, until this release redeploys the build job; the known-good line's template is
   the recorded one. The release's own record also captures the serving revision (`previous_revision`) and the
   template ACTIVE before its build (`active_before_build`): the pair a failure goes back to.
2. `bash deploy/release.sh --dry-run`.
3. `bash deploy/release.sh`: expect `known good`.
4. `bash deploy/release.sh --status`, the after capture: one `traffic:` line at 100% `[release <new id>]`, `serving:`
   the same, `ACTIVE:` the new release's template, both checks `yes`, and the previous revision
   `[release <the earlier live record>]`.
5. Commit the record as in step 9. Report the before and after lines as in "What to report".

#### What is proved where

| On real Cloud Run, by the drill | Covered only by automated tests (fake cloud or local database) |
|---|---|
| A new image tag read as not found from gcloud's real answer, so it is built (D0; A3) | The image step refusing a describe failure other than not found, building nothing (seen only by accident while signed out) |
| A first release whose build fails before its log is read goes back to no template ACTIVE; restore and `active` read the missing template table as `ACTIVE: none`; settle's list path against the real executions JSON, with an ended execution (D0; WR-06 path 1, WR-08) | The deploy fallback's accept path (a non-zero deploy whose own new revision is ready by digest) |
| A first build that activated, its log never read, undone by `restore --first` (DL; WR-06 path 2) | The split-traffic refusal |
| A service with no ready revision: `--status` says the next release is a first release, and D1 releases it as one (DU, then D1; WR-07 and A4) | A build execution that never started (WR-03); an unmatched running execution kept pending (WR-08's fail-closed branch) |
| Settle's list path while the build still runs, after a stop, and its wait loop (DK, `describe polls:` 1 or more; WR-08) | Cancelling a build still running after 5 minutes; a real unreadable execution state (DS forces it) |
| Traffic moved back while settle is unknown, the restore left for the rerun (DS; WR-11) | A mismatched or unverified restore, and a real failed restore job (D4's failure is forced, not a real job failure) |
| A half-done rollback resumed by the next `--rollback` (DR; WR-10) | A traffic move that does not read back; a kill during recovery |
| A non-zero first deploy with no ready revision is refused by the deploy fallback (DU; WR-09's check). Under pinned traffic, a revision that never becomes ready lets `gcloud run deploy` exit 0 and fails `--to-latest`, so nothing is routed (DW) | Missing scale values while `MIN_INSTANCES` is 1; rollback refusals; the breaking-template refusal; the release lock; a non-zero deploy whose latest ready revision is an earlier failed release's of the same image (WR-09; not reached on real cloud, where the deploy exits 0 under pinned traffic) |
| A non-zero traffic move counted on its read-back: update-traffic exits 1 while a revision that never became ready is the latest created one (DW; A5) | Forced failures at the other steps: add them with `--drill-fail-after <step>` on the drill; a forced failure after a step is not that step failing for real |
| The first release with no service; the serving-revision capture after a failed release; `--to-latest` after pinned traffic; recovery after a failure from smoke, the restore proved by its `ACTIVE:` line from Cloud Logging, traffic back and read back (D1, D2, D3; D5 and D6 when run) | The access fallback and refusal |
| A failed undo left pending and retried first by the next run, with no build (D4, when run); `--status` and `api.sandbox active` on the real image | |

### 12. Release day: the first product-mode release

The first release of the 06.1 product app (DEMO_MODE off, the HQ account) ships the WEB_REF named in
`06.1-HANDOFF.md` together with the release changes on build/deploy-hq. Neither reaches build/deploy before that day:
today's live app is the Phase 5 build and still needs demo mode on. In order:

1. Merge the build/web WEB_REF named in `06.1-HANDOFF.md` into build/deploy, by its hash, one way (build/web never
   takes build/deploy back).
2. Merge build/deploy-hq into build/deploy.
3. Confirm every assumption against `06.1-HANDOFF.md`, and adjust the one constant each names:
   `grep -rn 'ASSUMPTION A' deploy tests/deploy tests/deployed tests/api/test_smoke.py`
4. Under the shared test lock, run `make test`, `make test-api`, `make test-web` and `make web-check`, then
   `make compose-check`.
5. Run `make web-check` with `SHELFSHIFT_DENYLIST` set (your own terminal).
6. A drill release first (step 11). The drill copies the live configuration, so it proves the HQ preflight and the
   product checks on the disposable service.
7. Then the live release (step 7). When it refuses as a breaking template, that is step 7's compatibility rule: use
   `--breaking-template` only while no judge is using the app.

**The expected conflicts** (computed read-only on 2026-10-17 with `git merge-tree --write-tree --name-only
--no-messages build/deploy <branch>`, which touches no ref, index or worktree; build/deploy-hq merges with none):

- `api/app.py`: keep D-24's client address (the rightmost X-Forwarded-For on Cloud Run), the wrong-passcode 401's
  `attempts_left` and the SPA serving and its guard; keep the product's sign-in by account, the demo gate and its
  routes.
- `api/config.py`: keep the import-time `GLOBAL_AI_CAP` read (the unready drill depends on it), `WEB_DIST` and
  `ON_CLOUD_RUN`; keep the product's `HQ_PASSCODE`, `HQ_MIN_LENGTH` and the `DEMO_MODE` default off.
- `api/sandbox.py`: keep the release's `build`, `restore`, `--first` and `active`; keep the product's and 05.2's
  changes.
- `tests/api/test_caps.py`: both sides.
- `tests/web/test_journey.py`: take the product's file whole.
- `README.md`: both.
- Merged cleanly but check them: `api/settings.json` (the passcode cap stays D-24's 10 per clock minute, not the
  product's 20 per hour) and `api/caps.py` (the minute window).

For the real WEB_REF, rerun the computation with its hash before merging:

```zsh
git merge-tree --write-tree --name-only --no-messages build/deploy <the WEB_REF hash>
```

with the hash `06.1-HANDOFF.md` names in place of `<the WEB_REF hash>`, without the angle brackets. Where `06.1-HANDOFF.md`'s merge notes
differ from this list, the handoff wins.

## Not run in Phase 6

- The `AI.FORECAST` dry run and run: done in Phase 3.
- `pytest -m live` for Gemini, the smoke's chat ping and the SSE disconnect check: Phase 9.
- Switching `MIN_INSTANCES` to 1 at submission and back to 0 after 2026-11-06: the Phase 12 runbook (D-01). The
  release reads it from your configuration, so editing the key and releasing is the whole switch.

Migrations are additive only. A change that is not additive states its recovery step with it (D-17).
