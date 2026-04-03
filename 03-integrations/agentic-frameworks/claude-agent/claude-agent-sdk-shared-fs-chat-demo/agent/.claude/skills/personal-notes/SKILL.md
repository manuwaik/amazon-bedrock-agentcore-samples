---
name: personal-notes
description: Saves and retrieves personal notes synced to S3, scoped by user. Use when the user wants to save a note, remember something, or recall previously saved information.
---

# Personal Notes

Save and retrieve personal notes. Notes are synced to S3 and scoped by user ID, so each user's notes persist across sessions.

## Usage

```bash
python3 .claude/skills/personal-notes/scripts/note_manager.py save "Your note here"
python3 .claude/skills/personal-notes/scripts/note_manager.py read
```

## Key details

- Notes are stored in S3 at `users/{user_id}/notes.json` and cached locally at `/mnt/workspace/notes.json`
- S3 is the source of truth — reads always fetch from S3 first
- The `USER_ID` env var determines which user's notes to access
- Notes are stored as a JSON array of `{content, timestamp}` objects
