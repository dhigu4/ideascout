CRITICAL: PRODUCTION STATE IS PROTECTED

C:\Users\brad\IdeaScoutLocal is permanent production state -- the real
database, the real .env, real logs, and real backups. It is NOT part of
this repo and NOT a workspace.

NEVER, for any reason, including "cleaning up," "just checking," manual
smoke-testing, or verifying a fix:
- delete, recreate, overwrite, reset, or clean any file under IdeaScoutLocal
- run `rm`/`del`/`Remove-Item` (or any equivalent) against anything under it
- point a test, a manual script, or an ad-hoc verification command at it
- treat ideas.db, .env, app.log, or backups\ as disposable

All tests and all manual developer verification -- including anything
Claude runs directly in a terminal to "just check" something -- MUST use a
temporary directory (pytest's tmp_path, a tempfile.mkdtemp(), or similar).
If you need to see what a real run would do, build and inspect a temporary
database; never touch the real one to find out.

This rule exists because it was violated once already: repeated manual
smoke-testing against the production database's own default path, followed
by routine "clean up my test files" deletions, destroyed real production
data (15 messages, 9 feedback records) with no warning. See db.py's
open_production_database / ProductionDatabaseMissingError and the `init`
command for the resulting fail-loud safety net -- but the rule above is
the actual fix. Don't rely on the safety net catching a mistake; don't make
the mistake.

---

CRITICAL LOCAL CONFIG RULE

.env contains Brad's real local credentials and configuration. It lives
ONLY at C:\Users\brad\IdeaScoutLocal\.env -- never in the repo.

NEVER:
- modify .env
- delete .env
- overwrite .env
- recreate .env
- copy .env.example over .env
- move .env
- print or expose values from .env

You may modify .env.example when new configuration keys are required,
but .env itself is Brad-managed and must remain untouched.

If a new setting is required, tell Brad exactly which key to add manually
to .env.

If an old .env is found in the repo root (a leftover from before
persistent state moved to IdeaScoutLocal), warn Brad to move it there
himself. Do not move, copy, or delete it automatically.
