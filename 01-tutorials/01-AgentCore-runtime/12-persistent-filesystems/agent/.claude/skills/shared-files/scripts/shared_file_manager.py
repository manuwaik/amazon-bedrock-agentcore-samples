#!/usr/bin/env python3
"""Manage shared files with instant S3 sync for cross-session visibility."""

import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

SHARED_DIR = Path("/tmp/shared")
S3_PREFIX = "shared/"


def _get_bucket() -> str:
    bucket = os.environ.get("SHARED_FS_BUCKET")
    if not bucket:
        print(json.dumps({"status": "error", "message": "SHARED_FS_BUCKET env var not set"}))
        sys.exit(1)
    return bucket


def _s3():
    return boto3.client("s3")


def _safe_filename(filename: str) -> str:
    if ".." in filename or filename.startswith("/"):
        print(json.dumps({"status": "error", "message": "Invalid filename"}))
        sys.exit(1)
    resolved = (SHARED_DIR / filename).resolve()
    if not str(resolved).startswith(str(SHARED_DIR.resolve())):
        print(json.dumps({"status": "error", "message": "Invalid filename"}))
        sys.exit(1)
    return filename


def list_files():
    """List files directly from S3 for the freshest cross-session view."""
    bucket = _get_bucket()
    files = []
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                name = key.removeprefix(S3_PREFIX)
                if name:
                    files.append({
                        "name": name,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    })
    except ClientError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)
    print(json.dumps({"status": "success", "files": files, "count": len(files)}, indent=2, default=str))


def read_file(filename: str):
    """Read directly from S3 so the latest version is always returned."""
    filename = _safe_filename(filename)
    key = f"{S3_PREFIX}{filename}"
    try:
        obj = _s3().get_object(Bucket=_get_bucket(), Key=key)
        content = obj["Body"].read().decode("utf-8")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NoSuchKey":
            print(json.dumps({"status": "error", "message": f"File not found: {filename}"}))
        else:
            print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)
    # Update local cache
    local_path = SHARED_DIR / filename
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content)
    print(json.dumps({"status": "success", "filename": filename, "content": content}, indent=2))


def write_file(filename: str, content: str):
    """Write locally and immediately upload to S3 for instant cross-session visibility."""
    filename = _safe_filename(filename)
    local_path = SHARED_DIR / filename
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content)

    key = f"{S3_PREFIX}{filename}"
    try:
        _s3().put_object(Bucket=_get_bucket(), Key=key, Body=content.encode("utf-8"))
    except ClientError as e:
        print(json.dumps({"status": "error", "message": f"Local write ok but S3 upload failed: {e}"}))
        sys.exit(1)
    print(json.dumps({
        "status": "success",
        "message": "File written and synced to S3 — visible to all sessions immediately",
        "filename": filename,
        "size": len(content),
    }, indent=2))


def delete_file(filename: str):
    """Delete from S3 and remove local cache."""
    filename = _safe_filename(filename)
    key = f"{S3_PREFIX}{filename}"
    try:
        _s3().delete_object(Bucket=_get_bucket(), Key=key)
    except ClientError as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        sys.exit(1)
    local_path = SHARED_DIR / filename
    if local_path.exists():
        local_path.unlink()
    print(json.dumps({"status": "success", "message": f"Deleted {filename}"}))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: shared_file_manager.py <list|read|write|delete> [args...]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        list_files()
    elif cmd == "read" and len(sys.argv) >= 3:
        read_file(sys.argv[2])
    elif cmd == "write" and len(sys.argv) >= 4:
        write_file(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "delete" and len(sys.argv) >= 3:
        delete_file(sys.argv[2])
    else:
        print("Usage: shared_file_manager.py <list|read|write|delete> [args...]")
        sys.exit(1)