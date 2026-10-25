# Project Guidelines

## Architecture

- This is a Python 3.12+ Discord bot using discord.py 2.x.
- Put bot features in `cogs/` and load them through `main.py`.
- Use `database.py` for embedded PostgreSQL schema and persistence helpers. The PostgreSQL 17 data directory is `data/postgres/`; `bot.database` is the async `Database` pool facade. Do not issue feature queries directly through the pool.
- Keep `commands.json` synchronized with user-facing prefix and slash commands.

## Code Style

- Follow existing type annotations, Traditional Chinese user messages, and module-level logging patterns.
- Handle Discord permission/API failures explicitly and report failures to the user or logs; do not silently claim an operation succeeded.
- Defer slash-command interactions before slow Discord API work, then respond through the interaction follow-up.
- Preserve existing ownership checks before changing permissions, archiving, or deleting generated channels.
- Do not log bot tokens, full environment contents, or unnecessary personal data.

## Event Processing and Performance

- When changing Discord event handlers, keep the asyncio event loop responsive: do not add blocking I/O, long CPU-bound work, or synchronous waits to async handlers.
- Review `cogs/temp_voice.py` when investigating event-processing delays. Its voice-state handler serializes work with a lock and performs Discord API calls while holding it; avoid expanding the locked section or serializing independent work without a consistency requirement.
- Do not replace serialization with unbounded fire-and-forget tasks. Any concurrent or queued work must have a clear bound, lifecycle, error logging, and shutdown behavior, and must preserve channel ownership and cleanup guarantees.
- Prefer batching or avoiding redundant Discord API calls where behavior remains correct. Keep interaction responses within Discord's response deadline by deferring before slow API work.
- Keep PostgreSQL operations off the asyncio event loop. Use the asynchronous helpers in `database.py`; do not add direct synchronous database calls to async handlers.
- Design startup recovery and event-driven cleanup to tolerate reconnects and repeated event delivery; avoid duplicate side effects and preserve ownership tracking.
- Treat packet/event backlog or disconnect risk as a hypothesis until supported by runtime evidence. When addressing performance concerns, add or use measurements for handler duration, lock wait time, event-loop latency, and relevant Discord API failures; validate under concurrent-event scenarios without using a live bot token.

## Validation

- Compile changed Python modules with `py -m compileall -q main.py database.py cogs`.
- Validate `commands.json` with Python's `json` module and run `git diff --check`.
- Do not use a real bot token or make live Discord changes during local validation.
- When changing event, permission, or channel-ownership behavior, verify the existing authorization checks, ownership persistence, and cleanup behavior with focused tests or equivalent local validation.

## Security Scans

- For vulnerability scans or security reviews, follow [弱掃指南.md](./弱掃指南.md), including its scope, safety limits, and reporting requirements.
