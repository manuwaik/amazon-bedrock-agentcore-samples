"""Agent with a shared S3 filesystem skill.

Any file written via the shared-files skill is immediately synced to S3,
making it visible to all other sessions — regardless of which container
or AgentCore session handles the next request.

Per-session storage (/mnt/workspace) remains isolated between sessions,
demonstrating the contrast between private and shared storage.
"""

import os
from pathlib import Path

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from claude_agent_sdk.types import StreamEvent

MODEL_ID = "global.anthropic.claude-sonnet-4-6"

os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
os.environ["ANTHROPIC_MODEL"] = MODEL_ID

# Shared S3 bucket — derived from account ID to match the bucket created in the notebook
if not os.environ.get("SHARED_FS_BUCKET"):
    _account_id = boto3.client("sts").get_caller_identity()["Account"]
    os.environ["SHARED_FS_BUCKET"] = f"agentcore-shared-fs-{_account_id}"

# Persist Claude SDK session state on the per-session managed storage so the
# conversation context survives stop/resume within the same AgentCore session.
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
    previous_session = _load_session_id()

    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a helpful assistant with access to a shared filesystem.\n"
            "\n"
            "The shared filesystem is visible to ALL sessions — any file you write "
            "there is immediately available to every other agent session.\n"
            "Use the shared-files skill to read, write, list, and delete shared files.\n"
            "Be concise and confirm what you did after each operation.\n"
        ),
        cwd="/app",
        setting_sources=["project"],
        allowed_tools=["Skill", "Read", "Write", "Bash"],
        max_turns=10,
        resume=previous_session,
        include_partial_messages=True,
    )

    async for message in query(prompt=user_message, options=options):
        if isinstance(message, StreamEvent):
            yield message
            continue

        if isinstance(message, AssistantMessage):
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
