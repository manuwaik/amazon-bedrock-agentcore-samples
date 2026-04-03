#!/usr/bin/env python3
"""Personal note manager - saves notes locally and syncs to S3 scoped by user."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

NOTES_FILE = Path("/mnt/workspace/notes.json")


def _get_bucket() -> str:
    return os.environ["SHARED_FS_BUCKET"]


def _get_user_id() -> str:
    return os.environ.get("USER_ID", "default")


def _s3_key() -> str:
    return f"users/{_get_user_id()}/notes.json"


def _s3():
    return boto3.client("s3")


def _load_from_s3() -> list:
    """Load notes from S3 for the current user."""
    try:
        obj = _s3().get_object(Bucket=_get_bucket(), Key=_s3_key())
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return []
        raise


def _save_to_s3(notes: list):
    """Upload notes to S3 for the current user."""
    _s3().put_object(
        Bucket=_get_bucket(),
        Key=_s3_key(),
        Body=json.dumps(notes, indent=2).encode("utf-8"),
    )


def _save_local(notes: list):
    """Write notes to local workspace file."""
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTES_FILE, "w") as f:
        json.dump(notes, f, indent=2)


def save_note(content: str) -> None:
    # Load from S3 (source of truth) then append
    notes = _load_from_s3()

    note = {"content": content, "timestamp": datetime.now().isoformat()}
    notes.append(note)

    # Write to both local and S3
    _save_local(notes)
    _save_to_s3(notes)

    print(json.dumps({
        "status": "success",
        "message": f"Note saved for user '{_get_user_id()}'",
        "note": note,
    }, indent=2))


def read_notes() -> None:
    """Read notes from S3 and update local cache."""
    notes = _load_from_s3()
    _save_local(notes)

    print(json.dumps({
        "status": "success",
        "user_id": _get_user_id(),
        "notes": notes,
        "count": len(notes),
    }, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 note_manager.py <save|read> [note_content]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "save" and len(sys.argv) >= 3:
        save_note(" ".join(sys.argv[2:]))
    elif cmd == "read":
        read_notes()
    else:
        # Backwards compat: bare args = save
        save_note(" ".join(sys.argv[1:]))
