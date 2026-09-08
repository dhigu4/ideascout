# IdeaScout

**Stage 1** pulls messages from a dedicated AgentMail inbox and stores them,
raw and untouched, in a local SQLite database. No parsing, no AI, no
analysis happens there -- that's intentional. It exists so nothing is ever
lost or altered before later stages run.

**Stage 2** reads those raw emails and asks a cheap LLM to turn Brad's
explicit feedback (LIKE, PASS, NEW IDEA, etc.) into structured rows in a
second table, `feedback`. The raw email in `messages_raw` remains the
permanent source of truth; `feedback` is derived data that can always be
recomputed later if the parser or prompt improves. Stage 2 only ever
parses email from Brad's own approved addresses -- it never mistakes a
newsletter or forwarded article for his opinion.

## 0. Code vs. persistent state

Code lives in this repo (`C:\Users\brad\Repos\IdeaScout`). Everything real
-- the database, your credentials, logs, and backups -- lives entirely
outside it, at `C:\Users\brad\IdeaScoutLocal`:

```
C:\Users\brad\IdeaScoutLocal\
  ideas.db
  .env
  app.log
  backups\
```

This separation is deliberate and load-bearing: it's what makes it
impossible for a repo checkout, a test run, or routine cleanup to ever
touch production data again. See `CLAUDE.md` for the full rule, and
"Database initialization" below for how the database itself is protected.

## 1. Setup

Requires Python 3.10+ on Windows.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

(For running the test suite too: `pip install -r requirements-dev.txt` instead.)

## 2. AgentMail credentials

Create `C:\Users\brad\IdeaScoutLocal\.env` (the folder is created for you
automatically the first time you run any command, but you still need to
create `.env` yourself, once, with your real credentials -- copy
`.env.example` from this repo as a starting template):

```
AGENTMAIL_API_KEY=your-real-api-key
AGENTMAIL_INBOX_ID=your-inbox@yourdomain.agentmail.to
```

This `.env` is Brad-managed: IdeaScout only ever reads it, never writes,
moves, deletes, or prints it. `.env.example` (in the repo) is a template
with placeholders only and is safe to commit.

## 3. Initialize the database (one time)

```
python run.py init
```

Creates `IdeaScoutLocal\` (if needed), `IdeaScoutLocal\backups\`, and a
brand-new `ideas.db` with the current schema. This is the **only** command
allowed to create a new production database -- every other command
refuses outright if `ideas.db` is missing, and `init` itself refuses if a
database already exists at that path. You only need to run this once,
ever, per machine (see "Database initialization" below for why this
matters).

## 4. Running check-mail

```
python run.py check-mail
```

This connects to AgentMail, lists messages in the configured inbox, stores
any it hasn't seen before, and prints a summary:

```
AgentMail check complete
New messages found: 4
Stored successfully: 4
Already known: 7
Errors: 0
```

Running it again is always safe -- messages already stored are skipped, so
nothing is ever duplicated. Run this command as often as you like (e.g.
from Windows Task Scheduler every few minutes) to keep the database
current.

## 5. Running status

```
python run.py status
```

```
Database reachable: YES
Total raw messages: 14
Unparsed messages: 3
Parsed messages: 7
No-feedback messages: 1
Needs review: 1
Retryable errors: 0
Error messages: 0
Skipped (not a Brad sender): 1
Skipped (unauthenticated): 1
Feedback records: 9
Last successful inbox check: 2026-09-07T20:14:43+00:00
Last successful parse run: 2026-09-07T20:16:02+00:00
Last error: none
Data integrity warnings: 0
```

## 6. Where the database lives

`C:\Users\brad\IdeaScoutLocal\ideas.db` by default -- outside the repo,
created by `python run.py init` (see step 3). The log file lives alongside
it at `IdeaScoutLocal\app.log`, and timestamped backups live in
`IdeaScoutLocal\backups\` (see "Database backups" below). None of this is
part of the repo or committed to git.

You can point to a different location by setting `DATABASE_PATH` and/or
`LOG_PATH` in `.env` (see the commented-out examples in `.env.example`) --
but there's normally no reason to.

## 7. How to tell if the system is healthy

Run `python run.py status`. You're healthy if:

- `Database reachable: YES`
- `Last error: none` (or an old error you already know about)
- `Last successful inbox check` is recent (i.e., check-mail is actually
  being run regularly)
- `Data integrity warnings: 0`

If something looks wrong, open `IdeaScoutLocal\app.log` -- every error is
logged there with a full stack trace, in addition to the one-line summary
shown on screen. If `status` (or any other command) refuses to run and
reports the database missing or a possible data loss, **stop** -- don't
run `init`, don't delete anything, read the message (it lists any backups
found in `IdeaScoutLocal\backups\`) and investigate first.

## What Stage 1 deliberately does NOT do

No LLM calls, no parsing of email content, no fund-letter/Dropbox/website
processing, no investment screening, no vector database, no Docker, no web
dashboard, no cloud deployment. Every stored message starts with
`parse_status = UNPARSED` and is left exactly as received -- later stages
read from `messages_raw` without ever needing to touch Stage 1's code.

---

# Stage 2 -- turning feedback emails into structured records

## 1. Configuring the parser

Stage 2 needs two things in your `.env`:

```
ANTHROPIC_API_KEY=your-real-anthropic-key
BRAD_ALLOWED_SENDERS=brad@yourdomain.com
```

`ANTHROPIC_API_KEY` comes from the
[Anthropic Console](https://console.anthropic.com) -> API Keys.
`BRAD_ALLOWED_SENDERS` is one or more of Brad's own email addresses,
comma-separated -- **only email sent from one of these addresses, and
that AgentMail itself reports as authenticated, is ever handed to the
feedback parser.** A matching From address on its own is trivially
spoofable; requiring AgentMail's own SPF/DKIM/DMARC authentication result
too (see "Authentication is required" below) is what makes the sender
check actually mean something -- together they're what stops a newsletter
or forwarded Substack post that later lands in the same inbox from being
mistaken for Brad's own opinion. Both env vars are required for
`parse-mail`; `check-mail` and `status` don't need them.

By default Stage 2 uses `claude-haiku-4-5`, a fast, low-cost Claude model
well suited to this kind of short structured extraction. To use a
different model, set it explicitly:

```
# PARSER_MODEL_NAME=claude-haiku-4-5
```

The model/provider is isolated entirely inside `ideascout/parser.py` --
nothing else in the app knows what LLM provider is in use, so this can be
changed later without touching the database or CLI.

## 2. Running parse-mail

```
python run.py parse-mail
```

This looks at every message eligible for parsing -- never attempted, or a
previous attempt that failed for a transient reason -- asks the LLM to
extract feedback from each one, and prints a summary:

```
Feedback parse complete
Messages considered: 6
Parsed successfully: 3
No feedback found: 1
Needs review: 1
Retryable errors: 1
Errors: 0
Skipped (not a Brad sender): 1
Skipped (unauthenticated): 1
```

A message is never re-sent to the LLM once it already has feedback events
saved, so running this repeatedly is safe, cheap, and never creates
duplicate rows -- even for a message that produced several events (see
below).

If `ANTHROPIC_API_KEY` or `BRAD_ALLOWED_SENDERS` isn't set, the command
fails immediately with a clear message instead of silently doing nothing.

## 3. One email, multiple feedback events

Brad may reply to a digest of several ideas at once, e.g.:

```
1 LIKE - hidden asset is interesting
2 PASS - not enough upside
3 MAYBE - need to understand management
```

The parser returns a list of events (zero, one, or many) for each email,
and every event becomes its own row in `feedback`, numbered by
`event_index` (0, 1, 2, ...). All of one email's events are saved together
as a single all-or-nothing batch -- if saving is interrupted partway
through, none of that email's events are left half-saved, so a retry
starts clean instead of silently losing part of Brad's reply.

## 4. Authentication is required, not just a matching sender

A message is only ever sent to the LLM if **both** of these hold:

1. its sender is in `BRAD_ALLOWED_SENDERS`, and
2. AgentMail itself reported the message as authenticated (passed
   SPF/DKIM/DMARC).

AgentMail doesn't expose authentication as a field you can read off a
message -- only as a listing-time filter -- so `check-mail` figures it out
once, at capture time, by comparing two listings (one that includes
unauthenticated mail, one that doesn't) and records the result in
`messages_raw.sender_authenticated` (`AUTHENTICATED` or
`UNAUTHENTICATED`) so the decision is there to audit later. This never
happens again for a message already in the database -- it's recorded once,
at the moment the message is first captured.

If the sender matches but authentication didn't pass (or is unknown --
e.g. a message stored before this check existed), the message is marked
`SKIPPED_UNAUTHENTICATED` and never reaches the LLM. **Unknown never gets
the benefit of the doubt** -- it's treated exactly like a confirmed
failure, not like a pass. This is a policy decision, not a parser problem,
so it's never retried automatically and `requeue-feedback` refuses it too
(there's nothing a retry could change about it). It's a separate, visibly
distinct outcome from `SKIPPED_NOT_BRAD` in both `parse-mail`'s summary
and `status`, precisely so you can tell "wrong sender" apart from "right
sender, but AgentMail couldn't vouch for it."

## 5. Recognizing NO_FEEDBACK, NEEDS_REVIEW, RETRYABLE_ERROR, and ERROR

Run `python run.py status`. Several different things can keep a message
out of `Parsed messages`, and they mean different things:

- **`No feedback found`** -- the parser ran successfully and found no
  explicit feedback, new idea, or missed idea at all (e.g. a purely
  informational email). This is a normal, successful outcome, not a
  failure -- `PARSED` specifically means "at least one feedback row was
  committed," so a clean zero-event result gets its own status instead of
  being (wrongly) counted as parsed. **Not retried automatically** --
  there's nothing more for the LLM to find by trying again on the same
  text.
- **`Needs review`** -- the parser ran successfully but the result (or an
  event within it) needs a human look: it returned `UNCLEAR`, its
  confidence was below the review threshold, or its fields were
  internally inconsistent (e.g. a verdict-bearing event type with no
  verdict). These messages already have their feedback event(s) saved
  (including `UNCLEAR` ones) but are deliberately not counted as fully
  parsed. **Not retried automatically** -- ambiguous content won't become
  less ambiguous by asking the model again.
- **`Retryable errors`** -- a transient failure: a timeout, a connection
  error, HTTP 429 (rate limited), or a 5xx server error. **Retried
  automatically** the next time you run `parse-mail`, no action needed.
- **`Error messages`** -- a non-transient failure (e.g. invalid API
  credentials, a bad request). **Not retried automatically**, since
  retrying without a human fixing the underlying problem first would just
  waste API calls. Check `IdeaScoutLocal\app.log` for the reason, fix it,
  then use `requeue-feedback` (below) to give it another try.

`Skipped (not a Brad sender)` and `Skipped (unauthenticated)` are not
failures at all -- they mean the feedback parser was deliberately never
run on that email, because its sender isn't in `BRAD_ALLOWED_SENDERS`, or
because it is but AgentMail didn't confirm it as authenticated (see
"Authentication is required" above). Also visible in `status` is `Data
integrity warnings` -- normally `0`; a non-zero count means a message
ended up in a combination of status and feedback rows that should be
impossible (e.g. `PARSED` with no feedback row at all), and each one is
printed with enough detail to investigate. This is exactly the kind of
bug this app caught in itself once already (a one-time migration now
repairs it automatically on upgrade -- see the schema notes below) and
exists so a similar problem is never silently invisible again.

## 6. Requeuing a message for reprocessing

```
python run.py requeue-feedback MESSAGE_ID
python run.py requeue-feedback --all-errors
```

Use the first form after fixing whatever caused one message to end up in
`ERROR`, or to give a `NEEDS_REVIEW` message a genuine second attempt
(e.g. after improving the prompt). Either way it resets that message back
to `UNPARSED` and clears its error, so the next `parse-mail` run actually
calls the LLM again for it -- it never touches the raw email, which stays
byte-for-byte exactly as AgentMail delivered it.

Use `--all-errors` to requeue every message currently in `ERROR` in one
go. It intentionally leaves `NEEDS_REVIEW` and `SKIPPED_NOT_BRAD` messages
completely untouched -- those need a human decision or a config change,
not a blind retry.

A message already marked `PARSED`, or skipped as not-from-Brad, is
refused with a clear explanation rather than silently doing nothing --
and a `PARSED` message's confirmed feedback can never be reached by this
command at all, at any point.

One detail worth understanding: an `ERROR` message has no feedback rows
yet (the LLM call itself failed before anything was saved), so there is
nothing to clean up. A `NEEDS_REVIEW` message, though, already has its
feedback event(s) saved -- that's what triggered the review. Those rows
are derived, regenerable parser output (never Brad's own words), so
requeuing a `NEEDS_REVIEW` message removes them as part of the requeue.
This is what makes the fresh parse genuine: `parse-mail` never re-calls
the LLM for a message that already has feedback events, so leaving the
old ambiguous result in place would silently turn "requeue" into a no-op.
The command tells you how many superseded events it removed.

## 7. Running show-feedback

```
python run.py show-feedback
```

Prints the 10 most recent structured feedback events in plain text --
date, ticker/company, event type, verdict, Brad's comment, and the
parser's confidence -- so you never need to open the database directly to
see what's been captured. When one email produced more than one event,
each is labeled `(event 2)`, `(event 3)`, etc. A record excluded from
learning (see below) is labeled `[EXCLUDED FROM LEARNING]`. Pass `--limit
25` to see more.

## 8. Excluding a record from learning

```
python run.py exclude-feedback TICKER
python run.py include-feedback TICKER
```

Some feedback records shouldn't count when a later stage learns Brad's
preferences -- an artificial smoke-test record, for instance, or an
accidental duplicate. `exclude-feedback` finds every feedback record
whose `ticker` **or** `company` matches (case-insensitively -- not every
record has a ticker filled in), shows exactly what it found, and sets
`excluded_from_learning` on each one. `include-feedback` does the same
thing in reverse, for undoing an accidental exclusion.

```
$ python run.py exclude-feedback XYZ
Excluding from learning -- 1 matching feedback record(s) for 'XYZ':
  feedback_id=7 | XYZ | FEEDBACK | verdict=LIKE | "smoke test"
Done: 1 record(s) now excluded from learning (1 changed).
```

Neither command ever deletes anything -- not the raw email, not the
feedback record itself -- so excluded records stay fully visible for
audit/history in `show-feedback` and direct database inspection; they are
just flagged as unfit for training. Excluding one event in a multi-event
email only affects that one event's row, never the others, and never the
message's `feedback_parse_status`.

## 9. Raw email is always preserved

Stage 2 never modifies `subject`, `body_raw`, `sender`, or any other raw
field in `messages_raw` -- it only ever updates that message's
`feedback_parse_status`/`error`/`attempt_count`/`last_attempt_at` columns
and adds rows to the separate `feedback` table. This is also true for
email that isn't from an approved Brad sender: it is kept exactly as
received, just never interpreted as feedback. If parsing ever goes wrong,
or the extraction logic improves later, the original email is untouched
and available to re-parse. The LLM's extracted tags (reasons, concerns,
tickers, etc.) are hints for later stages, never a replacement for what
Brad actually wrote -- `user_comment` and the full raw `parsed_json` are
stored specifically so this can always be checked against the source
email.

## Project layout

```
run.py                        entry point: python run.py <command>
ideascout/
  cli.py                      init / check-mail / parse-mail / show-feedback / status / requeue-feedback / exclude-feedback / include-feedback commands
  agentmail_client.py         the only file that talks to the AgentMail SDK
  parser.py                   the only file that talks to the Anthropic (Claude) API
  db.py                       SQLite schema, migrations, backups, production-database safety, reads/writes
  config.py                   resolves paths under IdeaScoutLocal, loads .env from there
  logger.py                   sets up IdeaScoutLocal\app.log
tests/                        pytest suite -- always uses temporary directories (see "Testing" below)
.env.example                  template for IdeaScoutLocal\.env (no real secrets)
CLAUDE.md                     the production-state and .env protection rules -- read this first
```

Persistent state (`ideas.db`, `.env`, `app.log`, `backups\`) is **not**
part of this repo -- see "Code vs. persistent state" above.

## Testing

```
pip install -r requirements-dev.txt
pytest
```

The tests cover: database/schema creation (including upgrading a database
created under the previous schema, to prove migrations never lose data),
saving a message, duplicate prevention (running the same insert twice
never creates a second row), check-mail/parse-mail failure behavior (a
broken write is rejected and leaves no partial data; a failed AgentMail or
LLM connection is reported clearly instead of crashing), every feedback
vocabulary word parsing into the right column, one email producing
multiple feedback events with no duplicates on rerun, ambiguous/quoted
content landing in `NEEDS_REVIEW` (and never being retried automatically),
transient failures landing in `RETRYABLE_ERROR` and being retried
successfully on the next run, non-Brad senders being preserved but never
interpreted as feedback, status counts, requeue-feedback (an `ERROR`
message can be requeued and reprocessed; requeuing a `NEEDS_REVIEW`
message by ID removes its superseded derived event and causes the next
`parse-mail` run to genuinely call the LLM again, without duplicating
anything; `--all-errors` only touches `ERROR` messages and never
`NEEDS_REVIEW`; a `PARSED` message's confirmed feedback can never be
reached by a requeue; and the raw email is never altered by any of this),
the authentication gate (an authenticated Brad sender is parsed; an
unauthenticated one is skipped and preserved; unknown authentication is
never treated as a pass; a non-Brad sender is skipped as such regardless
of authentication; and none of this creates duplicates on rerun), the
data-integrity migration/health-check pair (a database with a message
stuck `PARSED` with zero feedback rows is repaired back to `UNPARSED` on
upgrade; a legitimately `PARSED` message with real feedback is left
untouched; the legacy `parse_status` column is never used to decide
`feedback_parse_status`; the repaired message becomes eligible for
`parse-mail` again; and rerunning migrations is a safe no-op), and the
`NO_FEEDBACK` status (a parser that successfully returns zero events
never gets marked `PARSED`; no feedback row is created for it; it is not
re-parsed on a later run; `PARSED` always requires at least one feedback
row, proven directly by parsing two messages side by side; an existing
database with the old `PARSED`/zero-feedback bug is repaired to
`NO_FEEDBACK` on upgrade without touching a legitimately `PARSED`
message; `NO_FEEDBACK` with zero feedback rows produces no integrity
warning while one with feedback rows is flagged as inconsistent; and the
raw email is never altered by any of this), and exclude-feedback/
include-feedback (matching by ticker or company, case-insensitively;
excluding leaves every other feedback row and the raw email untouched;
`include-feedback` correctly reverses an exclusion; the flag persists
across reconnects; and neither command ever deletes a message or a
feedback row). They also cover the production-state safety mechanisms
themselves (`tests/test_config.py`, `tests/test_production_state.py`):
production path defaults resolve outside this repo; an autouse fixture in
`tests/conftest.py` redirects every test's "production" paths into a
temporary sandbox, even ones that don't pass an explicit path;
`init` creates a fresh database and refuses to overwrite an existing one;
every normal command refuses to run against a missing database instead of
silently creating one; a database that previously had data and now has
less (or is missing outright) is detected and refused with a clear
message listing any backups found; pre-migration and routine backups are
created correctly (verified as real, restorable point-in-time snapshots,
not just files that exist); routine backup retention keeps the most
recent 14 while never touching pre-migration backups; and the production
`.env` is never written to by any code path. They run entirely offline
against temporary SQLite files with a fake parser -- no real AgentMail or
Anthropic account or credentials are needed to run `pytest`.

## Database initialization, safety, and backups

**Initialization (`python run.py init`)** is the only command allowed to
create a new production database. It refuses outright if a database
already exists at the target path, so it can never be used -- accidentally
or otherwise -- to wipe one. Every other command instead refuses to run at
all if the database is missing, rather than silently creating an empty
replacement: this is precisely the behavior that would have caught the
incident that motivated this design (see `CLAUDE.md`).

**Fail-loud safety.** A small sidecar file
(`IdeaScoutLocal\.ideascout_sentinel.json`) records the database's last
known message count. If a normal command finds the database missing, or
finds it now holds *fewer* messages than that record shows (message
counts only ever grow under normal operation), it refuses to proceed and
reports **possible data loss** -- along with the filenames of any backups
found in `IdeaScoutLocal\backups\`. No backup is ever restored
automatically; that decision is always left to a human.

**Backups** are made with SQLite's own online backup API
(`sqlite3.Connection.backup`), not a plain file copy -- safe to use even
while the source connection is open, unlike copying the file directly,
which could capture a half-written page mid-transaction. Two kinds are
made automatically, both under `IdeaScoutLocal\backups\`:

- **Pre-migration backups** (`ideas_<timestamp>_premigration.db`) -- made
  immediately before any schema migration is applied to a database that
  already has data. Never pruned.
- **Routine backups** (`ideas_<timestamp>_routine.db`) -- made after
  `check-mail`/`parse-mail`, rate-limited to at most one per calendar day.
  The most recent 14 are kept; older ones are pruned automatically.

The live database itself is never deleted by any of this.

## Database schema

Table `messages_raw` (one row per AgentMail message, never deleted or
overwritten -- Stage 2 never modifies these fields, only `parse_status`
and `error`):

| column        | meaning                                              |
|---------------|-------------------------------------------------------|
| message_id    | AgentMail's unique ID; `UNIQUE` -- this is what makes re-runs idempotent |
| inbox_id      | which inbox it came from |
| thread_id     | AgentMail thread ID |
| received_at   | timestamp AgentMail reports for the message |
| sender        | the `From` address |
| recipients    | the `To` address(es) |
| subject       | subject line |
| body_raw      | the verbatim message body, exactly as AgentMail returned it |
| body_format   | `text` or `html` -- which kind of content is in `body_raw` |
| size_bytes    | message size as reported by AgentMail |
| stored_at     | when *this program* wrote the row locally |
| parse_status  | Stage 1's original general-purpose status column; still set to `UNPARSED` at insert time but otherwise unused now -- see `feedback_parse_status` |
| feedback_parse_status | `UNPARSED`, `PARSED`, `NO_FEEDBACK`, `NEEDS_REVIEW`, `RETRYABLE_ERROR`, `ERROR`, `SKIPPED_NOT_BRAD`, or `SKIPPED_UNAUTHENTICATED` -- see "Recognizing NO_FEEDBACK..." and "Authentication is required" above |
| attempt_count | how many times the feedback parser has been run against this message |
| last_attempt_at | when the feedback parser was last attempted on this message |
| sender_authenticated | `AUTHENTICATED`, `UNAUTHENTICATED`, or `NULL` (unknown -- a message stored before this column existed; never treated as a pass) |
| error         | the parser's error message, if `feedback_parse_status` is `RETRYABLE_ERROR` or `ERROR` |

`parse_status` and `feedback_parse_status` are deliberately separate: a
later stage (e.g. parsing source documents like fund letters) will need
its own independent status on the same row, and keeping Stage 2's
feedback-parsing state in its own column now avoids the two ever being
confused.

Table `feedback` (Stage 2, zero or more rows per message -- one per
distinct feedback event found in that email; `UNIQUE(message_id,
event_index)` prevents any single event from being duplicated):

| column          | meaning                                              |
|-----------------|-------------------------------------------------------|
| feedback_id     | internal row id |
| message_id      | which raw email this came from (references `messages_raw`) |
| event_index     | 0, 1, 2, ... -- which event within that email this is, in extraction order |
| event_type      | `FEEDBACK`, `NEW_IDEA`, `MISSED_IDEA`, or `UNCLEAR` |
| verdict         | for `FEEDBACK`: `STRONG_LIKE`/`LIKE`/`MAYBE`/`PASS`/`STRONG_PASS`; otherwise usually `NULL` |
| ticker          | ticker Brad mentioned, if any |
| company         | company name Brad mentioned, if any |
| novelty         | `NEW`/`KNOWN`/`UNKNOWN`, if Brad said so |
| user_comment    | short summary of Brad's own comment, in his own words |
| parsed_json     | this event's complete raw LLM output, for audit and so the row can be regenerated later without re-reading the email |
| parser_version  | e.g. `feedback-v1` -- which parser logic produced this row |
| model_name      | which LLM model produced this row |
| confidence      | the parser's own 0.0-1.0 confidence estimate for this event |
| created_at      | when this feedback row was written |
| excluded_from_learning | `0` (default) or `1` -- set via `exclude-feedback`/`include-feedback`; a later stage that learns Brad's preferences must skip any row where this is `1` |

The schema is created and upgraded through a small migration list in
`ideascout/db.py` (`MIGRATIONS`). To change the schema later, add a new
entry to that list -- never edit an existing one -- so existing databases
upgrade safely without losing data. Two migrations in that list are data
repairs rather than schema changes, both for the same underlying rule
(`PARSED` must mean at least one feedback row was actually committed) hit
by two different bugs:

- An earlier migration bootstrapped `feedback_parse_status` from the
  legacy `parse_status` column, which could carry forward a stale
  `PARSED` value with no feedback row to back it up. That migration
  resets any such message back to `UNPARSED`, so it is genuinely
  reprocessed.
- A later bug in `parse-mail` itself marked a message `PARSED` even when
  the parser successfully ran but returned zero feedback events. That
  migration resets any such message to `NO_FEEDBACK` instead -- not
  `UNPARSED`, since these messages really were correctly processed; there
  is nothing to gain by re-running the LLM on them.

Both repairs run automatically and once, the next time the database is
opened, and neither ever touches a message that has one or more real
feedback rows. `status`'s `Data integrity warnings` line checks for this
same class of impossible state on an ongoing basis, so a similar bug can
never again go unnoticed.

Table `app_meta` is a simple key/value table used for `status` (e.g.
`last_check_at`, `last_parse_at`, `last_error`).
