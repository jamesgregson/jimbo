---
name: jot-that-down
description: Save something the user wants remembered across sessions. Use when the user says "remember", "jot that down", "note that", "from now on" or "always/never do X", or corrects how you work in a way that should last
---
Save the note with the `remember` tool. Jimbo reads saved notes at the start of every session.

## 1. Pick the scope

- **user** (`~/.jimbo/memory.md`, every project): the user's own preferences and habits. "I prefer short
  answers", "never commit for me", "I use uv, not pip".
- **project** (`.jimbo/memory.md` in this project): facts about this project. "Run tests with `make check`",
  "the API client is generated, don't edit it", "we chose SQLite over Postgres".

Go by what the user said: "in this repo" or "here" means project; "always", "everywhere" or "I" about a habit
means user. If it could be either and the difference matters, ask with `ask_user` (or in a message), with your
pick first. Don't save the same note in both.

## 2. Write the note

- One line, in plain words, that makes sense with no conversation around it.
- Keep the user's meaning exactly. Don't widen "don't edit X" into "be careful with generated files".
- Add the reason if the user gave one: "Don't mock the database in tests: a mocked test hid a broken migration."
- Use absolute dates, not "today" or "next week".

## 3. Don't duplicate

Read the memory sections in your instructions first. If a note already covers it, do nothing. If the new note
corrects or updates one, pass the old note's text as `replaces`. If the user says to forget something, ask them
to remove the line from the file, and tell them which file.

## 4. Don't save

- Anything that only matters for the current task.
- What the code, `AGENTS.md` or git history already says.
- Keys, passwords, tokens or personal data.

Then tell the user in one line what you saved and where.
