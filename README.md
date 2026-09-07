# IdeaScout -- Stage 1

Stage 1 does exactly one job: pull messages from a dedicated AgentMail
inbox and store them, raw and untouched, in a local SQLite database. No
parsing, no AI, no analysis happens yet -- that's intentional. This stage
exists so nothing is ever lost or altered before later stages get built.

## 1. Setup

Requires Python 3.10+ on Windows.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

(For running the test suite too: `pip install -r requirements-dev.txt` instead.)

## 2. AgentMail credentials

Copy `.env.example` to `.env` and fill in the two required values:

```
copy .env.example .env
```

Then edit `.env`:

```
AGENTMAIL_API_KEY=your-real-api-key
AGENTMAIL_INBOX_ID=your-inbox@yourdomain.agentmail.to
```

`.env` is listed in `.gitignore` and will never be committed. Never put
real credentials directly in code.

## 3. Running check-mail

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

## 4. Running status

```
python run.py status
```

```
Database reachable: YES
Total raw messages: 11
Unparsed messages: 11
Last successful inbox check: 2026-09-07T20:14:43+00:00
Last error: none
```

## 5. Where the database lives

`data/ideas.db`, inside the project folder, by default. The log file lives
alongside it at `data/app.log`. Both are created automatically on first
run. Neither is committed to git (see `.gitignore`).

You can point to a different location by setting `DATABASE_PATH` and/or
`LOG_PATH` in `.env` (see the commented-out examples in `.env.example`).

## 6. How to tell if the system is healthy

Run `python run.py status`. You're healthy if:

- `Database reachable: YES`
- `Last error: none` (or an old error you already know about)
- `Last successful inbox check` is recent (i.e., check-mail is actually
  being run regularly)

If something looks wrong, open `data/app.log` -- every error is logged
there with a full stack trace, in addition to the one-line summary shown
on screen.

## What Stage 1 deliberately does NOT do

No LLM calls, no parsing of email content, no fund-letter/Dropbox/website
processing, no investment screening, no vector database, no Docker, no web
dashboard, no cloud deployment. Every stored message starts with
`parse_status = UNPARSED` and is left exactly as received -- later stages
will read from `messages_raw` without ever needing to touch this stage's
code.

## Project layout

```
run.py                        entry point: python run.py <command>
ideascout/
  cli.py                      check-mail / status commands
  agentmail_client.py         the only file that talks to the AgentMail SDK
  db.py                       SQLite schema, migrations, reads/writes
  config.py                   loads .env
  logger.py                   sets up data/app.log
tests/                        pytest suite (see "Testing" below)
data/                         ideas.db + app.log (created at runtime, gitignored)
.env.example                  template for your .env (no real secrets)
```

## Testing

```
pip install -r requirements-dev.txt
pytest
```

The tests cover: database/schema creation, saving a message, duplicate
prevention (running the same insert twice never creates a second row), and
failure behavior (a broken write is rejected and leaves no partial data; a
failed AgentMail connection is reported clearly instead of crashing). They
run entirely offline against temporary SQLite files -- no real AgentMail
account or credentials are needed to run `pytest`.

## Database schema

Table `messages_raw` (one row per AgentMail message, never deleted or
overwritten):

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
| parse_status  | always `UNPARSED` in Stage 1; later stages will update this |
| error         | reserved for later parsing stages to record failures |

The schema is created and upgraded through a small migration list in
`ideascout/db.py` (`MIGRATIONS`). To change the schema later, add a new
entry to that list -- never edit an existing one -- so existing databases
upgrade safely without losing data.

Table `app_meta` is a simple key/value table used for `status` (e.g.
`last_check_at`, `last_error`).
