# Claude Agent SDK — S3-Backed Session Persistence

| Information         | Details                                                                      |
|---------------------|------------------------------------------------------------------------------|
| Agent type          | Asynchronous with Streaming                                                 |
| Agentic Framework   | Claude Agent SDK                                                           |
| LLM model           | Anthropic Claude (via Bedrock)                                              |
| Components          | AgentCore Runtime, Amazon S3                                                |
| Example complexity  | Easy-Medium                                                                 |
| SDK used            | Amazon BedrockAgentCore Python SDK, Claude Agent SDK, boto3                 |

This example demonstrates how to persist Claude Agent SDK sessions to S3 so that conversations survive container restarts, runtime version updates, and idle timeouts. Each `(user_id, conversation_id)` pair maps to an independent conversation with its own S3 prefix.

## Why S3-backed sessions?

The Claude Agent SDK stores conversation history as `.jsonl` files under `~/.claude/`. In AgentCore containers the filesystem is ephemeral. AgentCore's [managed session storage](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-runtime-session-storage.html) provides a persistent mount, but it **resets** on redeploy and after 14 days of inactivity. S3 avoids both limitations.

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer and resolver
- AWS account with Bedrock AgentCore access
- Node.js and npm (for Claude Code CLI)
- An S3 bucket for session storage

## Setup Instructions

### 1. Create a Python Environment with uv

```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 2. Install Requirements

```bash
uv pip install -r requirements.txt
```

### 3. Create an S3 Bucket and IAM Role

```bash
export SESSION_BUCKET="claude-sessions-$(aws sts get-caller-identity --query Account --output text)"
aws s3 mb s3://$SESSION_BUCKET

# Pre-create the execution role with S3 permissions baked in
python setup_role.py $SESSION_BUCKET
```

The script creates an IAM role (`ClaudeSessionsAgentCoreRole`) with the base `BedrockAgentCoreFullAccess` managed policy plus S3 read/write/list permissions scoped to your session bucket. If the role already exists it updates the trust and S3 policies in place.

### 4. Configure and Launch with Bedrock AgentCore Toolkit

```bash
# Configure with the pre-created role (copy the ARN from setup_role.py output)
agentcore configure -e agent.py --disable-memory \
  --execution-role arn:aws:iam::<account-id>:role/ClaudeSessionsAgentCoreRole

# Deploy your agent
agentcore deploy \
  --env CLAUDE_CODE_USE_BEDROCK=1 \
  --env SESSION_BUCKET=$SESSION_BUCKET
```

### 5. Testing Your Agent

The payload takes two fields that control session isolation:

- **`user_id`** (required) — Identifies the user. Each user's conversations are stored under their own S3 prefix.
- **`conversation_id`** (required) — Identifies a specific conversation within that user. Each conversation gets its own independent history.

Together they form the S3 namespace: `s3://{bucket}/{user_id}/{conversation_id}/.claude/`. See [`agent.py#L148`](agent.py#L148) where both fields are extracted from the payload.

```bash
# Start a conversation about payments
agentcore invoke '{"prompt": "My name is Alice and I work on the payments team.", "user_id": "alice", "conversation_id": "payments-chat"}'

# Resume the same conversation — Alice's context is preserved
agentcore invoke '{"prompt": "What is my name and what team do I work on?", "user_id": "alice", "conversation_id": "payments-chat"}'

# Start a separate conversation for Alice — independent history
agentcore invoke '{"prompt": "Help me plan our team offsite.", "user_id": "alice", "conversation_id": "offsite-planning"}'

# Different user — bob has his own namespace, no access to Alice's conversations
agentcore invoke '{"prompt": "What is my name?", "user_id": "bob", "conversation_id": "intro"}'
```

The second invocation should recall Alice's name and team, demonstrating that the session was persisted to S3 and restored. The third invocation (different `conversation_id`) should *not* have payments context. The fourth invocation (different `user_id`) should know nothing about Alice, confirming full isolation.

### Aligning with AgentCore Runtime sessions

AgentCore Runtime assigns its own session IDs via the `runtimeSessionId` header (see [Runtime sessions docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)). Each runtime session gets a dedicated microVM with a persistent filesystem for its lifecycle (up to 8 hours, 15 min idle timeout).

We recommend **using the same ID for both `runtimeSessionId` and `conversation_id`**. When they match:
- The runtime routes repeated invocations to the same microVM (filesystem already warm)
- The agent detects the conversation is already loaded on disk and **skips the S3 restore** entirely
- S3 sync only kicks in when the microVM is recycled (idle timeout, lifecycle end, redeploy)

When they don't match, everything still works correctly — the agent clears `~/.claude/` and restores from S3 on each conversation switch. You just lose the skip-restore optimisation.

## How it Works

### Session lifecycle

The Claude Agent SDK stores conversation history as `.jsonl` files under `~/.claude/`. Since AgentCore containers have ephemeral filesystems, the agent syncs these files to/from S3 around each invocation. Here is the full lifecycle, with links to the relevant code:

1. **Clear** ([`agent.py#L76-L79`](agent.py#L76-L79)) — Before restoring, `clear_claude_home` removes any leftover `~/.claude/` directory. This prevents session data from a different conversation leaking into the current one. Skipped if the marker file shows the same conversation is already loaded (see [Skip-restore optimisation](#skip-restore-optimisation)).

2. **Restore** ([`agent.py#L82-L100`](agent.py#L82-L100)) — `restore_sessions` downloads the conversation's session files from `s3://{bucket}/{user_id}/{conversation_id}/.claude/` into `~/.claude/`. On the very first invocation for a conversation there is nothing in S3, so this is a no-op.

3. **Resume** ([`agent.py#L157-L161`](agent.py#L157-L161)) — The agent reads the stored `session_id` for this `(user_id, conversation_id)` pair. If one exists, it passes `ClaudeAgentOptions(resume=session_id)` so the SDK picks up the prior conversation context. For a brand-new conversation, `resume` is `None` and a fresh session is created.

4. **Query & capture** ([`agent.py#L167-L174`](agent.py#L167-L174)) — The prompt is sent via `query()`. As the response streams back, the agent yields each message. When the final `ResultMessage` arrives, its `session_id` is persisted locally so the next invocation can resume from it.

5. **Save** ([`agent.py#L103-L119`](agent.py#L103-L119)) — After the response completes, `save_sessions` uploads the contents of `~/.claude/` back to S3. This includes the updated `.jsonl` history and the session ID file.

### Skip-restore optimisation

A marker file at `/tmp/.last_conversation` ([`agent.py#L37`](agent.py#L37)) tracks which `user_id/conversation_id` is currently loaded on disk. When an invocation arrives for the same conversation that's already loaded, the agent skips the clear and restore steps entirely. This avoids redundant S3 round-trips when the same conversation is invoked repeatedly on the same microVM — the common case when `runtimeSessionId` and `conversation_id` are aligned.

The marker file lives in `/tmp` (not `~/.claude/`) so it survives the clear step but is naturally lost when the microVM is recycled.

### Filtering and safety

Not everything under `~/.claude/` should be synced. The agent skips `node_modules/`, cache directories, lock files, and log files (see [`SKIP_PATTERNS`](agent.py#L42)). Individual files larger than 10 MB are also excluded ([`MAX_FILE_SIZE`](agent.py#L36)). During restore, a path-traversal check ([`agent.py#L93-L95`](agent.py#L93-L95)) ensures downloaded keys cannot escape the `~/.claude/` directory.

### Latency impact

The S3 sync adds overhead at the start and end of each invocation, but in practice the impact is small:

- **Restore** performs a `ListObjectsV2` followed by parallel-ish `GetObject` calls. For a typical session (a handful of small `.jsonl` files totalling < 100 KB) this takes **50–200 ms** from within the same AWS region.
- **Save** uploads the same set of files. Because the SDK appends to existing `.jsonl` files rather than creating many new ones, the file count stays low and upload time is similar: **50–200 ms**.
- **Skip-restore**: When the marker file matches, restore is skipped entirely — **0 ms** overhead on the read side.
- **Net effect**: ~100–400 ms of added latency per invocation (or ~50–200 ms with skip-restore), which is negligible compared to the LLM inference time (typically several seconds). The overhead grows linearly with session file count, but the skip/size filters keep this bounded.

If latency becomes a concern for very long-lived sessions, you could add delta-based syncing (only upload changed files) or move to S3 Express One Zone for single-digit-ms object access.

See the [Claude Agent SDK sessions documentation](https://platform.claude.com/docs/en/agent-sdk/sessions) for details on `resume`, `fork`, and `ClaudeSDKClient`.

## Clean Up

```bash
# Destroy the agent and all its associated AWS resources
agentcore destroy

# Empty and delete the S3 bucket
aws s3 rb s3://$SESSION_BUCKET --force
```
