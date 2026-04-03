import logging
import os
from pathlib import Path

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
from config import S3_BUCKET_PREFIX, MODEL_ID
from claude_agent_sdk import (
    query, AssistantMessage, ResultMessage, TextBlock, ClaudeAgentOptions,
)
from claude_agent_sdk.types import StreamEvent

os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
os.environ["ANTHROPIC_MODEL"] = MODEL_ID

# S3 bucket for shared and per-user storage (derived once, used by all skill scripts)
if not os.environ.get("SHARED_FS_BUCKET"):
    _account_id = boto3.client("sts").get_caller_identity()["Account"]
    os.environ["SHARED_FS_BUCKET"] = f"{S3_BUCKET_PREFIX}-{_account_id}"

# Persist Claude SDK session files on the mounted filesystem so they survive restarts
WORKSPACE = Path("/mnt/workspace")
CLAUDE_PERSISTENT = WORKSPACE / ".claude"
CLAUDE_HOME = Path.home() / ".claude"
SESSION_ID_FILE = WORKSPACE / ".claude_session_id"


def _setup_persistent_sessions():
    """Symlink ~/.claude -> /mnt/workspace/.claude so session .jsonl files persist."""
    CLAUDE_PERSISTENT.mkdir(parents=True, exist_ok=True)
    if CLAUDE_HOME.is_symlink():
        return
    if CLAUDE_HOME.exists():
        # Move existing data into the persistent location, then symlink
        import shutil
        for item in CLAUDE_HOME.iterdir():
            dest = CLAUDE_PERSISTENT / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))
        CLAUDE_HOME.rmdir()
    CLAUDE_HOME.symlink_to(CLAUDE_PERSISTENT)


def _load_session_id() -> str | None:
    if SESSION_ID_FILE.exists():
        sid = SESSION_ID_FILE.read_text().strip()
        return sid if sid else None
    return None


def _save_session_id(sid: str):
    SESSION_ID_FILE.write_text(sid)


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    _setup_persistent_sessions()

    user_message = payload.get("prompt", "Hello!")
    user_id = payload.get("user_id", "default")
    os.environ["USER_ID"] = user_id

    previous_session = _load_session_id()

    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a helpful personal assistant.\n"
            "\n"
            "When the user asks about information you don't have in conversation history, "
            "check your personal notes and shared files before saying you don't know.\n"
            "Be concise and helpful. Don't ask clarifying questions when the intent is clear.\n"
        ),
        cwd="/app",
        setting_sources=["project"],
        allowed_tools=["Skill", "Read", "Write", "Bash", "Grep", "Glob"],
        max_turns=20,
        resume=previous_session,
        include_partial_messages=True,
    )

    async for message in query(prompt=user_message, options=options):
        if isinstance(message, StreamEvent):
            # Forward streaming events (text deltas, tool use indicators) to the runtime
            yield message
            continue

        if isinstance(message, AssistantMessage):
            # Skip sub-responses from internal tool execution (e.g. Skill tool reading SKILL.md)
            if message.parent_tool_use_id is not None:
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Agent: {block.text}")
        if isinstance(message, ResultMessage) and message.session_id:
            _save_session_id(message.session_id)
        yield message


if __name__ == "__main__":
    app.run()
