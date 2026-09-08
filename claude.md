CRITICAL LOCAL CONFIG RULE

.env contains Brad's real local credentials and configuration.

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