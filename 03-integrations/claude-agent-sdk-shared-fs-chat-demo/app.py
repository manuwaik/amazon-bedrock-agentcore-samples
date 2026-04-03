"""
Generic chat frontend for any AgentCore Runtime agent.

Streams responses via SSE and provides session management
(new session, stop/resume, run commands in the container).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agentcore-chat")
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

# ----- Config ----------------------------------------------------------------
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
if not AGENT_RUNTIME_ARN:
    print(
        "ERROR: AGENT_RUNTIME_ARN env var is not set.\n"
        "Deploy an agent first, then:\n"
        '  AGENT_RUNTIME_ARN="arn:aws:..." python app.py',
        file=sys.stderr,
    )
    sys.exit(1)

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

S3_BUCKET_PREFIX = "agentcore-claude-filesytem-demo"
S3_SHARED_PREFIX = "shared/"

def _get_shared_bucket() -> str:
    bucket = os.environ.get("SHARED_FS_BUCKET")
    if not bucket:
        account_id = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
        bucket = f"{S3_BUCKET_PREFIX}-{account_id}"
    return bucket
# -----------------------------------------------------------------------------

app = FastAPI(title="AgentCore Chat")
client = boto3.client("bedrock-agentcore", region_name=REGION)

SESSIONS_FILE = Path(__file__).parent / "sessions.json"
MESSAGES_DIR = Path(__file__).parent / "session_messages"
MESSAGES_DIR.mkdir(exist_ok=True)


def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text())
    return {}


def _save_sessions(sessions: dict):
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def _track_session(session_id: str, label: str | None = None):
    sessions = _load_sessions()
    if session_id not in sessions:
        sessions[session_id] = {
            "label": label or "",
            "created": datetime.now(timezone.utc).isoformat(),
        }
        _save_sessions(sessions)


def _messages_file(session_id: str) -> Path:
    return MESSAGES_DIR / f"{session_id}.json"


def _load_messages(session_id: str) -> list:
    f = _messages_file(session_id)
    if f.exists():
        return json.loads(f.read_text())
    return []


def _append_message(session_id: str, role: str, text: str):
    msgs = _load_messages(session_id)
    msgs.append({"role": role, "text": text})
    _messages_file(session_id).write_text(json.dumps(msgs, indent=2))


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "default"


class ExecRequest(BaseModel):
    command: str
    timeout: int = 30


class SessionLabel(BaseModel):
    label: str


@app.get("/", response_class=HTMLResponse)
def index():
    with open("index.html") as f:
        return f.read()


@app.post("/chat")
def chat(req: ChatRequest):
    def generate():
        try:
            kwargs = {
                "agentRuntimeArn": AGENT_RUNTIME_ARN,
                "qualifier": "DEFAULT",
                "payload": json.dumps({"prompt": req.message, "user_id": req.user_id}),
            }
            if req.session_id:
                kwargs["runtimeSessionId"] = req.session_id

            response = client.invoke_agent_runtime(**kwargs)
            session_id = response.get("runtimeSessionId", "")

            _track_session(session_id)
            _append_message(session_id, "user", req.message)
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

            assistant_text = ""
            current_tool = None
            in_tool = False
            after_tool = False
            for raw_line in response["response"].iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8")
                if not line.startswith("data: "):
                    log.debug("[SSE non-data] %s", line[:200])
                    continue
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    log.warning("[SSE bad JSON] %s", line[:200])
                    continue

                # --- StreamEvent: has "event" key with raw Claude API streaming events ---
                if "event" in data and isinstance(data["event"], dict):
                    # Skip sub-agent streaming (internal tool execution)
                    if data.get("parent_tool_use_id") is not None:
                        continue
                    event = data["event"]
                    event_type = event.get("type")

                    if event_type == "content_block_start":
                        content_block = event.get("content_block", {})
                        if content_block.get("type") == "tool_use":
                            current_tool = content_block.get("name", "tool")
                            in_tool = True
                            yield f"data: {json.dumps({'type': 'tool_start', 'name': current_tool})}\n\n"

                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and not in_tool:
                            chunk = delta.get("text", "")
                            if chunk:
                                if after_tool and assistant_text and not assistant_text.endswith("\n"):
                                    assistant_text += "\n\n"
                                    yield f"data: {json.dumps({'type': 'text_delta', 'text': chr(10) + chr(10)})}\n\n"
                                    after_tool = False
                                assistant_text += chunk
                                yield f"data: {json.dumps({'type': 'text_delta', 'text': chunk})}\n\n"

                    elif event_type == "content_block_stop":
                        if in_tool:
                            yield f"data: {json.dumps({'type': 'tool_done', 'name': current_tool})}\n\n"
                            current_tool = None
                            in_tool = False
                            after_tool = True

                    continue

                # --- AssistantMessage fallback (complete messages) ---
                is_assistant = "model" in data
                if "content" in data:
                    for item in data["content"]:
                        if "text" in item and is_assistant:
                            # Only forward if we haven't already streamed this text
                            if not assistant_text:
                                assistant_text += item["text"] + "\n"
                                yield f"data: {json.dumps({'type': 'text', 'text': item['text']})}\n\n"

            if assistant_text.strip():
                _append_message(session_id, "assistant", assistant_text.strip())

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/sessions/{session_id}/exec")
def exec_command(session_id: str, req: ExecRequest):
    """Run a shell command inside the agent container for a given session."""
    try:
        response = client.invoke_agent_runtime_command(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            body={"command": req.command, "timeout": req.timeout},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    stdout, stderr = "", ""
    exit_code = None
    for event in response["stream"]:
        if "chunk" not in event:
            continue
        chunk = event["chunk"]
        if "contentDelta" in chunk:
            stdout += chunk["contentDelta"].get("stdout", "")
            stderr += chunk["contentDelta"].get("stderr", "")
        if "contentStop" in chunk:
            exit_code = chunk["contentStop"].get("exitCode")

    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


@app.get("/sessions")
def list_sessions():
    """Return all saved sessions."""
    return _load_sessions()


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    """Return saved chat messages for a session."""
    return _load_messages(session_id)


@app.patch("/sessions/{session_id}")
def rename_session(session_id: str, req: SessionLabel):
    """Set a friendly label on a session."""
    sessions = _load_sessions()
    if session_id in sessions:
        sessions[session_id]["label"] = req.label
        _save_sessions(sessions)
    return {"ok": True}


@app.post("/sessions/{session_id}/stop")
def stop_session(session_id: str):
    """Stop a runtime session (keeps session and message history for resume)."""
    try:
        client.stop_runtime_session(
            runtimeSessionId=session_id,
            agentRuntimeArn=AGENT_RUNTIME_ARN,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"stopped": session_id}


@app.get("/shared-files")
def list_shared_files():
    """List files in the shared S3 folder."""
    try:
        bucket = _get_shared_bucket()
        s3 = boto3.client("s3", region_name=REGION)
        files = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=S3_SHARED_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                name = key.removeprefix(S3_SHARED_PREFIX)
                if name:
                    files.append({"name": name, "size": obj["Size"], "last_modified": obj["LastModified"].isoformat()})
        return {"bucket": bucket, "files": files}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/shared-files/{filename:path}")
def get_shared_file(filename: str):
    """Get the contents of a file from the shared S3 folder."""
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        bucket = _get_shared_bucket()
        s3 = boto3.client("s3", region_name=REGION)
        key = f"{S3_SHARED_PREFIX}{filename}"
        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj["Body"].read().decode("utf-8")
        return {"filename": filename, "content": content}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a session and its message history."""
    sessions = _load_sessions()
    sessions.pop(session_id, None)
    _save_sessions(sessions)
    msg_file = _messages_file(session_id)
    if msg_file.exists():
        msg_file.unlink()
    return {"deleted": session_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
