---
name: shared-files
description: Reads, writes, lists, and deletes files in /tmp/shared/ — a cross-session directory synced to S3. Use when the user asks about shared files, wants to share content across sessions, or asks about information that might be in shared storage.
---

# Shared Files

Manage files in `/tmp/shared/` — visible to **all** agent sessions via S3 sync.

## Usage

Always use the manager script for instant S3 sync:

```bash
python3 .claude/skills/shared-files/scripts/shared_file_manager.py list
python3 .claude/skills/shared-files/scripts/shared_file_manager.py read <filename>
python3 .claude/skills/shared-files/scripts/shared_file_manager.py write <filename> <content>
python3 .claude/skills/shared-files/scripts/shared_file_manager.py delete <filename>
```

## Key details

- Always use `shared_file_manager.py` — not Read/Write tools on `/tmp/shared/` directly
- Direct Read/Write on `/tmp/shared/` only affects local cache (up to 5 min sync delay)
- The `list` command queries S3 directly for the freshest view of available files
