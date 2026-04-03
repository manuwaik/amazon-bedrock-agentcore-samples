---
name: shared-files
description: Reads, writes, lists, and deletes files in the shared S3 filesystem. Use when the user asks to read, write, list, or delete shared files, or when content needs to be visible across sessions.
---

# Shared Files

Manage files in the shared S3 filesystem — visible to **all** agent sessions instantly.

## Usage

Always use the manager script so changes are immediately synced to S3:

```bash
python3 .claude/skills/shared-files/scripts/shared_file_manager.py list
python3 .claude/skills/shared-files/scripts/shared_file_manager.py read <filename>
python3 .claude/skills/shared-files/scripts/shared_file_manager.py write <filename> <content>
python3 .claude/skills/shared-files/scripts/shared_file_manager.py delete <filename>
```

## Key details

- Always go through `shared_file_manager.py` — never use Read/Write tools on `/tmp/shared/` directly
- `list` and `read` query S3 directly, so you always see the latest state from any session
- `write` uploads to S3 immediately — another session can read it right away