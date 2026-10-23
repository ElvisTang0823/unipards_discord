# Project Guidelines

## Architecture

- This is a Python 3.10+ Discord bot using discord.py 2.x.
- Put bot features in `cogs/` and load them through `bot.py`.
- Use `database.py` for SQLite schema and persistence helpers. `bot.database` is a `sqlite3.Connection`; do not call feature-specific methods on the connection.
- Keep `commands.json` synchronized with user-facing prefix and slash commands.

## Code Style

- Follow existing type annotations, Traditional Chinese user messages, and module-level logging patterns.
- Handle Discord permission/API failures explicitly and report failures to the user or logs; do not silently claim an operation succeeded.
- Defer slash-command interactions before slow Discord API work, then respond through the interaction follow-up.
- Preserve existing ownership checks before changing permissions, archiving, or deleting generated channels.

## Validation

- Compile changed Python modules with `py -m compileall -q bot.py database.py cogs`.
- Validate `commands.json` with Python's `json` module and run `git diff --check`.
- Do not use a real bot token or make live Discord changes during local validation.