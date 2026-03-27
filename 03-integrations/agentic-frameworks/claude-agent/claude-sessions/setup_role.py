#!/usr/bin/env python3
"""Pre-create an AgentCore execution role with S3 session bucket permissions.

Usage:
    export SESSION_BUCKET="claude-sessions-$(aws sts get-caller-identity --query Account --output text)"
    python setup_role.py              # uses SESSION_BUCKET env var
    python setup_role.py my-bucket    # or pass bucket name directly
"""

import json
import sys

import boto3

ROLE_NAME = "ClaudeSessionsAgentCoreRole"


def main():
    bucket = sys.argv[1] if len(sys.argv) > 1 else None
    if not bucket:
        import os
        bucket = os.environ.get("SESSION_BUCKET")
    if not bucket:
        print("Usage: python setup_role.py <bucket-name>")
        print("   or: export SESSION_BUCKET=<bucket-name> && python setup_role.py")
        sys.exit(1)

    sts = boto3.client("sts")
    iam = boto3.client("iam")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    region = boto3.session.Session().region_name or "us-east-1"

    # Trust policy for AgentCore
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/*"
                    },
                },
            }
        ],
    }

    # Create or update the role
    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="AgentCore execution role with S3 session persistence",
        )
        print(f"Created role: {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(
            RoleName=ROLE_NAME,
            PolicyDocument=json.dumps(trust_policy),
        )
        print(f"Role already exists, updated trust policy: {ROLE_NAME}")

    # Attach base AgentCore managed policy
    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess",
    )

    # Add S3 session bucket permissions
    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SessionBucketReadWrite",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
            {
                "Sid": "SessionBucketList",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
            },
        ],
    }

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="SessionBucketAccess",
        PolicyDocument=json.dumps(s3_policy),
    )

    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    print(f"S3 permissions added for bucket: {bucket}")
    print(f"\nRole ARN: {role_arn}")
    print(f"\nNext steps:")
    print(f"  agentcore configure -e agent.py --disable-memory --execution-role {role_arn}")
    print(f"  agentcore deploy --env CLAUDE_CODE_USE_BEDROCK=1 --env SESSION_BUCKET={bucket}")


if __name__ == "__main__":
    main()
