# Claude Agent SDK — S3-Backed Session Persistence

| Information         | Details                                                                      |
|---------------------|------------------------------------------------------------------------------|
| Agent type          | Asynchronous with Streaming                                                 |
| Agentic Framework   | Claude Agent SDK                                                           |
| LLM model           | Anthropic Claude (via Bedrock)                                              |
| Components          | AgentCore Runtime, Amazon S3                                                |
| Example complexity  | Easy-Medium                                                                 |
| SDK used            | Amazon BedrockAgentCore Python SDK, Claude Agent SDK, boto3                 |

This example demonstrates how to persist Claude Agent SDK sessions to S3 so that conversations survive container restarts, runtime version updates, and idle timeouts. Each `user_id` gets an isolated S3 prefix for session isolation.

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

The `user_id` field controls session isolation. Each unique `user_id` gets its own S3 prefix (`s3://{bucket}/{user_id}/.claude/`), so conversations are stored and restored independently. If you omit `user_id`, it defaults to `"default"`. See [`agent.py#L113`](agent.py#L113) where `user_id` is extracted from the payload and used as the namespace for `restore_sessions` and `save_sessions`.

```bash
# First invocation — creates a new session for alice
agentcore invoke '{"prompt": "My name is Alice and I work on the payments team.", "user_id": "alice"}'

# Second invocation — resumes alice's session with full context
agentcore invoke '{"prompt": "What is my name and what team do I work on?", "user_id": "alice"}'

# Different user — bob has his own session, so the agent has no memory of Alice
agentcore invoke '{"prompt": "What is my name?", "user_id": "bob"}'
```

The second invocation should recall Alice's name and team, demonstrating that the session was persisted to S3 and restored. The third invocation should *not* know Bob's name, confirming that sessions are isolated per user.

## How it Works

### Session lifecycle

The Claude Agent SDK stores conversation history as `.jsonl` files under `~/.claude/`. Since AgentCore containers have ephemeral filesystems, the agent syncs these files to/from S3 around each invocation. Here is the full lifecycle, with links to the relevant code:

1. **Restore** ([`agent.py#L45-L67`](agent.py#L45-L67)) — Before the conversation starts, `restore_sessions` downloads the user's session files from `s3://{bucket}/{user_id}/.claude/` into `~/.claude/`. On the very first invocation for a user there is nothing in S3, so this is a no-op.

2. **Resume** ([`agent.py#L119-L123`](agent.py#L119-L123)) — The agent reads the stored `session_id` for this user (a small file written locally during the previous save). If one exists, it passes `ClaudeAgentOptions(resume=session_id)` so the SDK picks up the prior conversation context. For a brand-new user, `resume` is `None` and a fresh session is created.

3. **Query & capture** ([`agent.py#L125-L132`](agent.py#L125-L132)) — The prompt is sent via `query()`. As the response streams back, the agent yields each message. When the final `ResultMessage` arrives, its `session_id` is persisted locally so the next invocation can resume from it.

4. **Save** ([`agent.py#L70-L88`](agent.py#L70-L88)) — After the response completes, `save_sessions` uploads the contents of `~/.claude/` back to S3. This includes the updated `.jsonl` history and the session ID file.

### Filtering and safety

Not everything under `~/.claude/` should be synced. The agent skips `node_modules/`, cache directories, lock files, and log files (see [`SKIP_PATTERNS`](agent.py#L29)). Individual files larger than 10 MB are also excluded ([`MAX_FILE_SIZE`](agent.py#L26)). During restore, a path-traversal check ([`agent.py#L60-L62`](agent.py#L60-L62)) ensures downloaded keys cannot escape the `~/.claude/` directory.

### Latency impact

The S3 sync adds overhead at the start and end of each invocation, but in practice the impact is small:

- **Restore** performs a `ListObjectsV2` followed by parallel-ish `GetObject` calls. For a typical session (a handful of small `.jsonl` files totalling < 100 KB) this takes **50–200 ms** from within the same AWS region.
- **Save** uploads the same set of files. Because the SDK appends to existing `.jsonl` files rather than creating many new ones, the file count stays low and upload time is similar: **50–200 ms**.
- **Net effect**: ~100–400 ms of added latency per invocation, which is negligible compared to the LLM inference time (typically several seconds). The overhead grows linearly with session file count, but the skip/size filters keep this bounded.

If latency becomes a concern for very long-lived sessions, you could add delta-based syncing (only upload changed files) or move to S3 Express One Zone for single-digit-ms object access.

See the [Claude Agent SDK sessions documentation](https://platform.claude.com/docs/en/agent-sdk/sessions) for details on `resume`, `fork`, and `ClaudeSDKClient`.

## Clean Up

```bash
# Destroy the agent and all its associated AWS resources
agentcore destroy

# Empty and delete the S3 bucket
aws s3 rb s3://$SESSION_BUCKET --force
```
