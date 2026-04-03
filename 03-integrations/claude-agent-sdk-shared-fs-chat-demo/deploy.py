"""
Deploy the chat-demo agent to AgentCore Runtime.

  python deploy.py              # deploy
  python deploy.py --destroy    # tear down
"""

import argparse, base64, json, subprocess, sys, time
import boto3
from boto3.session import Session

from config import S3_BUCKET_PREFIX

RUNTIME_NAME  = "chat_demo_session_agent"
ECR_REPO      = "chat-demo-session-agent"
ROLE_NAME     = "ChatDemoAgentCoreRole"
POLICY_NAME   = "ChatDemoAgentCorePolicy"
MOUNT_PATH    = "/mnt/workspace"

sess       = Session()
region     = sess.region_name
account_id = boto3.client("sts").get_caller_identity()["Account"]
iam        = boto3.client("iam")
ecr        = boto3.client("ecr", region_name=region)
s3         = boto3.client("s3", region_name=region)
acc        = boto3.client("bedrock-agentcore-control", region_name=region)

SHARED_BUCKET = f"{S3_BUCKET_PREFIX}-{account_id}"

TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": ["bedrock-agentcore.amazonaws.com"]},
    "Action": "sts:AssumeRole",
    "Condition": {
        "StringEquals": {"aws:SourceAccount": account_id},
        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"},
    },
}]}

POLICY = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": [
        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken",
    ], "Resource": "*"},
    {"Effect": "Allow", "Action": [
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
        "logs:DescribeLogGroups", "logs:DescribeLogStreams",
    ], "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"},
    {"Effect": "Allow", "Action": [
        "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
    ], "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        f"arn:aws:bedrock:{region}:{account_id}:*",
    ]},
    {"Effect": "Allow", "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ], "Resource": [
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:workload-identity-directory/default",
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:workload-identity-directory/default/workload-identity/*",
    ]},
    {"Effect": "Allow", "Action": [
        "xray:PutTraceSegments", "xray:PutTelemetryRecords",
        "xray:GetSamplingRules", "xray:GetSamplingTargets",
    ], "Resource": "*"},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    {"Effect": "Allow", "Action": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
    ], "Resource": [
        f"arn:aws:s3:::{S3_BUCKET_PREFIX}-{account_id}",
        f"arn:aws:s3:::{S3_BUCKET_PREFIX}-{account_id}/*",
    ]},
]}


def ensure_role() -> str:
    policy_arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"
    role_exists = False
    try:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        role_exists = True
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST),
        )["Role"]["Arn"]
        print(f"Created IAM role: {role_arn}")

    # Create or update the policy document
    try:
        iam.get_policy(PolicyArn=policy_arn)
        # Policy exists — create a new version with updated permissions
        iam.create_policy_version(
            PolicyArn=policy_arn,
            PolicyDocument=json.dumps(POLICY),
            SetAsDefault=True,
        )
        # Clean up old non-default versions (max 5 allowed)
        versions = iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]
        for v in versions:
            if not v["IsDefaultVersion"]:
                iam.delete_policy_version(PolicyArn=policy_arn, VersionId=v["VersionId"])
        print("Updated IAM policy")
    except iam.exceptions.NoSuchEntityException:
        iam.create_policy(PolicyName=POLICY_NAME, PolicyDocument=json.dumps(POLICY))
        print("Created IAM policy")

    if not role_exists:
        iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
        time.sleep(10)  # propagation

    return role_arn


def ensure_s3_bucket() -> str:
    try:
        s3.head_bucket(Bucket=SHARED_BUCKET)
        print(f"S3 bucket exists: {SHARED_BUCKET}")
    except s3.exceptions.ClientError:
        create_kwargs = {"Bucket": SHARED_BUCKET}
        if region != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**create_kwargs)
        print(f"Created S3 bucket: {SHARED_BUCKET}")
    return SHARED_BUCKET


def ensure_ecr() -> str:
    try:
        return ecr.create_repository(repositoryName=ECR_REPO)["repository"]["repositoryUri"]
    except ecr.exceptions.RepositoryAlreadyExistsException:
        return ecr.describe_repositories(repositoryNames=[ECR_REPO])["repositories"][0]["repositoryUri"]


def docker_push(repo_uri: str):
    token = base64.b64decode(
        ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    ).decode().split(":")[1]
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    subprocess.run(f"echo {token} | docker login --username AWS --password-stdin {registry}",
                   shell=True, check=True)
    image = f"{repo_uri}:latest"
    # Copy shared config into agent build context
    import shutil
    shutil.copy("config.py", "agent/config.py")
    subprocess.run(["docker", "build", "-t", image, "agent"], check=True)
    subprocess.run(["docker", "push", image], check=True)
    return image


def find_runtime() -> str | None:
    for r in acc.list_agent_runtimes().get("agentRuntimes", []):
        if r["agentRuntimeName"] == RUNTIME_NAME:
            return r["agentRuntimeId"]
    return None


def deploy_runtime(image: str, role_arn: str) -> tuple[str, str]:
    kwargs = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        roleArn=role_arn,
        protocolConfiguration={"serverProtocol": "HTTP"},
        networkConfiguration={"networkMode": "PUBLIC"},
        filesystemConfigurations=[{"sessionStorage": {"mountPath": MOUNT_PATH}}],
    )
    rid = find_runtime()
    if rid:
        resp = acc.update_agent_runtime(agentRuntimeId=rid, **kwargs)
    else:
        resp = acc.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, **kwargs)
        rid = resp["agentRuntimeId"]

    print("Waiting for READY", end="", flush=True)
    for _ in range(60):
        st = acc.get_agent_runtime(agentRuntimeId=rid)["status"]
        if st == "READY": break
        if "FAIL" in st:
            print(f"\nRuntime failed: {st}"); sys.exit(1)
        print(".", end="", flush=True); time.sleep(5)
    print(f" {st}")
    return rid, resp["agentRuntimeArn"]


def deploy():
    role_arn = ensure_role()
    bucket = ensure_s3_bucket()
    repo_uri = ensure_ecr()
    image = docker_push(repo_uri)
    _, arn = deploy_runtime(image, role_arn)
    print(f'\nDone!\n  AGENT_RUNTIME_ARN="{arn}" python app.py')
    print(f"  Shared filesystem bucket: {bucket}")


def destroy_s3_bucket():
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=SHARED_BUCKET):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=SHARED_BUCKET, Key=obj["Key"])
        s3.delete_bucket(Bucket=SHARED_BUCKET)
        print(f"Deleted S3 bucket: {SHARED_BUCKET}")
    except Exception:
        pass


def destroy():
    rid = find_runtime()
    if rid:
        acc.delete_agent_runtime(agentRuntimeId=rid)
        print(f"Deleted runtime: {rid}")
    policy_arn = f"arn:aws:iam::{account_id}:policy/{POLICY_NAME}"
    for fn in [
        lambda: iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn),
        lambda: iam.delete_role(RoleName=ROLE_NAME),
    ]:
        try: fn()
        except Exception: pass
    # Delete all policy versions before deleting the policy
    try:
        versions = iam.list_policy_versions(PolicyArn=policy_arn)["Versions"]
        for v in versions:
            if not v["IsDefaultVersion"]:
                iam.delete_policy_version(PolicyArn=policy_arn, VersionId=v["VersionId"])
        iam.delete_policy(PolicyArn=policy_arn)
    except Exception:
        pass
    destroy_s3_bucket()
    print("Clean-up complete.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--destroy", action="store_true")
    if p.parse_args().destroy:
        destroy()
    else:
        deploy()
