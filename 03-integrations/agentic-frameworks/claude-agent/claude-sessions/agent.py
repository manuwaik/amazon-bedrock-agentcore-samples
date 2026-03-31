#!/usr/bin/env python3
"""Claude Agent SDK with S3-backed session persistence.

Persists Claude SDK session files (~/.claude/) to S3 so conversations
survive container restarts, runtime version updates, and idle timeouts.

Each (user_id, conversation_id) pair maps to an independent Claude SDK
session.  The S3 layout is:

    s3://{bucket}/{user_id}/{conversation_id}/.claude/...

A marker file at /tmp/.last_conversation keeps track of which
conversation is currently loaded on disk, allowing the agent to skip
redundant S3 restores when the same conversation is invoked repeatedly
on the same microVM.

Flow: clear -> restore from S3 -> resume session -> query -> save to S3
"""

import os
import shutil
from pathlib import Path

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

S3_BUCKET = os.environ["SESSION_BUCKET"]
CLAUDE_HOME = Path.home() / ".claude"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MARKER_FILE = Path("/tmp/.last_conversation")

# Files/directories to skip during S3 sync
SKIP_PATTERNS = ("node_modules/", ".cache/", ".npm/", "*.log", "*.lock")


def _should_skip(relative_path: str) -> bool:
    for pattern in SKIP_PATTERNS:
        if pattern.endswith("/"):
            if relative_path.startswith(pattern) or f"/{pattern}" in relative_path:
                return True
        elif pattern.startswith("*"):
            if relative_path.endswith(pattern[1:]):
                return True
        elif relative_path == pattern:
            return True
    return False


def _s3_prefix(user_id: str, conversation_id: str) -> str:
    return f"{user_id}/{conversation_id}/.claude/"


def _read_marker() -> str | None:
    """Read the marker file that tracks which conversation is loaded on disk."""
    if MARKER_FILE.exists():
        value = MARKER_FILE.read_text().strip()
        return value if value else None
    return None


def _write_marker(user_id: str, conversation_id: str):
    """Write a marker so subsequent invocations can skip restore."""
    MARKER_FILE.write_text(f"{user_id}/{conversation_id}")


def clear_claude_home():
    """Remove ~/.claude/ to prevent cross-conversation contamination."""
    if CLAUDE_HOME.exists():
        shutil.rmtree(CLAUDE_HOME)
        print("[sessions] Cleared ~/.claude/")


def restore_sessions(s3, user_id: str, conversation_id: str):
    """Download session files from S3 to ~/.claude/."""
    prefix = _s3_prefix(user_id, conversation_id)
    paginator = s3.get_paginator("list_objects_v2")

    total = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            relative = obj["Key"][len(prefix):]
            if not relative or _should_skip(relative) or obj["Size"] > MAX_FILE_SIZE:
                continue

            local_path = CLAUDE_HOME / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Path traversal protection
            if not str(local_path.resolve()).startswith(str(CLAUDE_HOME.resolve())):
                continue

            s3.download_file(S3_BUCKET, obj["Key"], str(local_path))
            total += 1

    print(f"[sessions] Restored {total} file(s) from s3://{S3_BUCKET}/{prefix}")


def save_sessions(s3, user_id: str, conversation_id: str):
    """Upload session files from ~/.claude/ to S3."""
    if not CLAUDE_HOME.exists():
        return

    prefix = _s3_prefix(user_id, conversation_id)
    total = 0

    for file_path in CLAUDE_HOME.rglob("*"):
        if not file_path.is_file():
            continue
        relative = str(file_path.relative_to(CLAUDE_HOME))
        if _should_skip(relative) or file_path.stat().st_size > MAX_FILE_SIZE:
            continue

        s3.upload_file(str(file_path), S3_BUCKET, f"{prefix}{relative}")
        total += 1

    print(f"[sessions] Saved {total} file(s) to s3://{S3_BUCKET}/{prefix}")


def _session_id_file(user_id: str, conversation_id: str) -> Path:
    return CLAUDE_HOME / f".session_id_{user_id}_{conversation_id}"


def load_session_id(user_id: str, conversation_id: str) -> str | None:
    f = _session_id_file(user_id, conversation_id)
    if f.exists():
        sid = f.read_text().strip()
        return sid if sid else None
    return None


def save_session_id(user_id: str, conversation_id: str, sid: str):
    _session_id_file(user_id, conversation_id).parent.mkdir(parents=True, exist_ok=True)
    _session_id_file(user_id, conversation_id).write_text(sid)


def _session_exists(session_id: str) -> bool:
    """Check if a Claude SDK session transcript (.jsonl) exists on disk.

    The SDK stores transcripts at ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl.
    resume crashes with ProcessError if the file doesn't exist, so we must
    check before passing it.
    """
    projects_dir = CLAUDE_HOME / "projects"
    if not projects_dir.exists():
        return False
    for cwd_dir in projects_dir.iterdir():
        if (cwd_dir / f"{session_id}.jsonl").exists():
            return True
    return False


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    user_id = payload["user_id"]
    conversation_id = payload["conversation_id"]
    conversation_key = f"{user_id}/{conversation_id}"
    s3 = boto3.client("s3")

    # Skip restore if this microVM already has the right conversation loaded
    if _read_marker() == conversation_key:
        print(f"[sessions] Conversation {conversation_key} already on disk, skipping restore")
    else:
        # Clear stale session data from a different conversation
        clear_claude_home()
        # Restore this conversation's session state from S3
        restore_sessions(s3, user_id, conversation_id)
        _write_marker(user_id, conversation_id)

    # Only resume if the session transcript exists on disk — the SDK throws
    # ProcessError if asked to resume a non-existent session.
    previous_session = load_session_id(user_id, conversation_id)
    if previous_session and not _session_exists(previous_session):
        print(f"[sessions] Session {previous_session} not found on disk, starting fresh")
        previous_session = None

    options = ClaudeAgentOptions(
        system_prompt="You are a helpful assistant.",
        resume=previous_session,
    )

    async for message in query(prompt=payload.get("prompt", "Hello!"), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Claude: {block.text}")
        if isinstance(message, ResultMessage) and message.session_id:
            save_session_id(user_id, conversation_id, message.session_id)
        yield message

    # Persist session state to S3
    save_sessions(s3, user_id, conversation_id)


if __name__ == "__main__":
    app.run()
