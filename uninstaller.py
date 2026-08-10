#!/usr/bin/env python3
"""
AWS Infrastructure Uninstaller
Deletes all AWS resources created by installer.py.
"""

import argparse
import json
import logging
import sys
import time

import boto3
from botocore.exceptions import ClientError

# Configuration (must match installer.py)
project_name = "rag-graph"
region = "us-west-2"
AGENTCORE_GATEWAY_REGION = "us-east-1"
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
vector_index_name = project_name
neptune_graph_name = project_name
# Shared with agent-skills / other rag-project apps
SHARED_RAG_PROJECT = "rag-project"
cloudfront_comment = f"CloudFront-for-{SHARED_RAG_PROJECT}"
oai_comment = f"OAI for {SHARED_RAG_PROJECT}"

sts_client = boto3.client("sts", region_name=region)
account_id = sts_client.get_caller_identity()["Account"]

knowledge_base_name = project_name
knowledge_base_role_name = f"role-knowledge-base-for-{project_name}-{region}"
bucket_name = f"storage-for-{SHARED_RAG_PROJECT}-{account_id}-{region}"

s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
secrets_client = boto3.client("secretsmanager", region_name=region)
neptune_graph_client = boto3.client("neptune-graph", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=AGENTCORE_GATEWAY_REGION,
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


logger = setup_logging()
def _matches_cloudfront(dist: dict) -> bool:
    return cloudfront_comment in dist.get("Comment", "")


def disable_cloudfront_distributions():
    """Disable CloudFront distributions created by installer."""
    logger.info("[1/6] Disabling CloudFront distributions")

    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if not dist.get("Enabled", True):
                logger.info(f"  Distribution already disabled: {dist['Id']}")
                continue

            dist_id = dist["Id"]
            logger.info(f"  Disabling distribution: {dist_id}")
            config_response = cloudfront_client.get_distribution_config(Id=dist_id)
            config = config_response["DistributionConfig"]
            config["Enabled"] = False
            cloudfront_client.update_distribution(
                Id=dist_id,
                DistributionConfig=config,
                IfMatch=config_response["ETag"],
            )

        logger.info("✓ CloudFront distributions disabled (deployment may take several minutes)")
    except Exception as e:
        logger.error(f"Error disabling CloudFront distributions: {e}")


def wait_for_cloudfront_disabled(max_wait: int = 900, poll_interval: int = 30):
    """Wait until project CloudFront distributions are fully disabled."""
    logger.info("  Waiting for CloudFront distributions to become disabled...")

    waited = 0
    while waited < max_wait:
        still_enabled = []
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if _matches_cloudfront(dist) and dist.get("Enabled", True):
                still_enabled.append(dist["Id"])

        if not still_enabled:
            logger.info("  ✓ All matching CloudFront distributions are disabled")
            return True

        logger.info(
            f"  Still enabled: {still_enabled} ({waited}s/{max_wait}s)"
        )
        time.sleep(poll_interval)
        waited += poll_interval

    logger.warning("  Timed out waiting for CloudFront to disable; delete step may be skipped")
    return False


def delete_cloudfront_distributions():
    """Delete disabled CloudFront distributions."""
    logger.info("[6/6] Deleting CloudFront distributions")

    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if dist.get("Enabled", True):
                logger.info(f"  Skipping enabled distribution: {dist['Id']}")
                continue

            dist_id = dist["Id"]
            try:
                config_response = cloudfront_client.get_distribution_config(Id=dist_id)
                cloudfront_client.delete_distribution(
                    Id=dist_id,
                    IfMatch=config_response["ETag"],
                )
                logger.info(f"  ✓ Deleted distribution: {dist_id}")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "DistributionNotDisabled":
                    logger.info(f"  Distribution {dist_id} is not fully disabled yet, skipping")
                elif code == "NoSuchDistribution":
                    logger.debug(f"  Distribution {dist_id} already deleted")
                else:
                    logger.warning(f"  Could not delete distribution {dist_id}: {e}")

        logger.info("✓ CloudFront distributions processed")
    except Exception as e:
        logger.error(f"Error deleting CloudFront distributions: {e}")


def delete_cloudfront_oai():
    """Delete Origin Access Identity created for the RAG project."""
    logger.info("  Deleting CloudFront Origin Access Identities")

    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get("Items", []):
            if oai_comment not in oai.get("Comment", ""):
                continue
            oai_id = oai["Id"]
            try:
                config_response = cloudfront_client.get_cloud_front_origin_access_identity_config(
                    Id=oai_id
                )
                cloudfront_client.delete_cloud_front_origin_access_identity(
                    Id=oai_id,
                    IfMatch=config_response["ETag"],
                )
                logger.info(f"  ✓ Deleted OAI: {oai_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchCloudFrontOriginAccessIdentity":
                    logger.debug(f"  OAI {oai_id} already deleted")
                else:
                    logger.warning(f"  Could not delete OAI {oai_id}: {e}")
    except Exception as e:
        logger.warning(f"  Error deleting OAI: {e}")


def delete_knowledge_base(knowledge_base_id: str) -> None:
    """Delete a Knowledge Base and its data sources (mirrors installer.py)."""
    try:
        try:
            data_sources = bedrock_agent_client.list_data_sources(
                knowledgeBaseId=knowledge_base_id,
                maxResults=100,
            )
            for ds in data_sources.get("dataSourceSummaries", []):
                try:
                    bedrock_agent_client.delete_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds["dataSourceId"],
                    )
                    logger.info(f"    ✓ Deleted data source: {ds['dataSourceId']}")
                except Exception as e:
                    logger.warning(
                        f"    Could not delete data source {ds['dataSourceId']}: {e}"
                    )
        except Exception as e:
            logger.debug(f"    Error listing/deleting data sources: {e}")

        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"  ✓ Deleted Knowledge Base: {knowledge_base_id}")

        max_wait = 120
        waited = 0
        while waited < max_wait:
            try:
                kb_response = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=knowledge_base_id
                )
                status = kb_response["knowledgeBase"]["status"]
                if status == "DELETED":
                    break
                time.sleep(5)
                waited += 5
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    break
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.debug(f"  Knowledge Base {knowledge_base_id} already deleted")
        else:
            logger.warning(f"  Could not delete Knowledge Base {knowledge_base_id}: {e}")


def delete_knowledge_bases():
    """Delete Knowledge Bases created by installer."""
    logger.info("[2/6] Deleting Knowledge Bases")

    try:
        kb_list = bedrock_agent_client.list_knowledge_bases()
        kb_to_delete = [
            kb["knowledgeBaseId"]
            for kb in kb_list.get("knowledgeBaseSummaries", [])
            if kb["name"] == knowledge_base_name
        ]

        if not kb_to_delete:
            logger.info(f"  No Knowledge Base found with name: {knowledge_base_name}")
            return

        for kb_id in kb_to_delete:
            logger.info(f"  Deleting Knowledge Base: {kb_id}")
            delete_knowledge_base(kb_id)

        logger.info("✓ Knowledge Bases deleted")
    except Exception as e:
        logger.error(f"Error deleting Knowledge Bases: {e}")


def _empty_s3_bucket(bucket: str):
    """Remove all objects and versions from an S3 bucket."""
    delete_keys = []

    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        for version in page.get("Versions", []):
            delete_keys.append(
                {"Key": version["Key"], "VersionId": version["VersionId"]}
            )
        for marker in page.get("DeleteMarkers", []):
            delete_keys.append(
                {"Key": marker["Key"], "VersionId": marker["VersionId"]}
            )

    if not delete_keys:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                delete_keys.append({"Key": obj["Key"]})

    if not delete_keys:
        return

    for i in range(0, len(delete_keys), 1000):
        batch = delete_keys[i : i + 1000]
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch})

    logger.info(f"  ✓ Deleted {len(delete_keys)} objects from {bucket}")


def delete_s3_buckets():
    """Delete S3 bucket created by installer."""
    logger.info("[5/6] Deleting S3 buckets")

    for bucket in [bucket_name]:
        try:
            try:
                s3_client.head_bucket(Bucket=bucket)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchBucket", "NotFound"):
                    logger.info(f"  Bucket {bucket} does not exist")
                    continue
                raise

            try:
                _empty_s3_bucket(bucket)
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchBucket":
                    logger.warning(f"  Could not empty bucket {bucket}: {e}")

            try:
                s3_client.delete_bucket_policy(Bucket=bucket)
                logger.info(f"  ✓ Removed bucket policy from {bucket}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchBucketPolicy":
                    logger.debug(f"  No bucket policy on {bucket}: {e}")

            s3_client.delete_bucket(Bucket=bucket)
            logger.info(f"  ✓ Deleted bucket: {bucket}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucket":
                logger.info(f"  Bucket {bucket} does not exist")
            else:
                logger.warning(f"  Could not delete bucket {bucket}: {e}")

    logger.info("✓ S3 buckets deleted")


def prompt_yes_no(question: str, default: bool = False) -> bool:
    """Prompt for yes/no. Empty input returns default."""
    suffix = " [Y/n]" if default else " [y/N]"
    response = input(f"{question}{suffix}: ").strip().lower()
    if not response:
        return default
    return response in ("y", "yes")


def delete_neptune_analytics_graph():
    """Delete Neptune Analytics graph used by GraphRAG Knowledge Base.

    Delete Knowledge Base first (caller responsibility), then the graph,
    otherwise Neptune may continue billing after KB deletion.
    """
    logger.info("[2/6] Deleting Neptune Analytics graph")

    try:
        graph_id = None
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = neptune_graph_client.list_graphs(**kwargs)
            for graph in response.get("graphs", []):
                if graph.get("name") == neptune_graph_name:
                    graph_id = graph["id"]
                    break
            if graph_id:
                break
            next_token = response.get("nextToken")
            if not next_token:
                break

        if not graph_id:
            logger.info(f"  Neptune graph not found: {neptune_graph_name}")
            return

        try:
            detail = neptune_graph_client.get_graph(graphIdentifier=graph_id)
            if detail.get("deletionProtection"):
                logger.info("  Disabling deletion protection on Neptune graph...")
                neptune_graph_client.update_graph(
                    graphIdentifier=graph_id,
                    deletionProtection=False,
                )
        except ClientError as e:
            logger.warning(f"  Could not update deletion protection: {e}")

        neptune_graph_client.delete_graph(
            graphIdentifier=graph_id,
            skipSnapshot=True,
        )
        logger.info(f"  ✓ Deleted Neptune graph: {neptune_graph_name} ({graph_id})")

        # Wait briefly for deletion to start
        for _ in range(12):
            try:
                status = neptune_graph_client.get_graph(graphIdentifier=graph_id).get("status")
                if status == "DELETING":
                    logger.info("  Neptune graph deletion in progress...")
                time.sleep(10)
            except ClientError as e:
                if e.response["Error"]["Code"] in (
                    "ResourceNotFoundException",
                    "GraphNotFoundException",
                ):
                    logger.info("  ✓ Neptune graph deletion confirmed")
                    break
                raise

        logger.info("✓ Neptune Analytics graph deleted")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "GraphNotFoundException"):
            logger.info(f"  Neptune graph already deleted: {neptune_graph_name}")
        else:
            logger.error(f"Error deleting Neptune graph: {e}")
    except Exception as e:
        logger.error(f"Error deleting Neptune graph: {e}")


def _list_all_agentcore_gateways():
    gateways = []
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore_control_client.list_gateways(**kwargs)
        gateways.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
    return gateways

def _list_all_agentcore_gateway_targets(gateway_id: str):
    targets = []
    next_token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore_control_client.list_gateway_targets(**kwargs)
        targets.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
    return targets

def delete_agentcore_websearch_gateway(confirmed: bool = False) -> bool:
    """Delete AgentCore gateway-websearch and its web-search targets.

    Returns True when the gateway was deleted or did not exist.
    Returns False when the user declined deletion or deletion failed.
    """
    logger.info("[3/6] Deleting AgentCore Web Search gateway")

    if not confirmed:
        logger.info(
            "  Skipping AgentCore Web Search gateway deletion (default: no)."
        )
        return False

    gateway_id = None
    try:
        for gateway in _list_all_agentcore_gateways():
            if gateway.get("name") == AGENTCORE_WEBSEARCH_GATEWAY_NAME:
                gateway_id = gateway["gatewayId"]
                logger.info(
                    f"  Found gateway: {AGENTCORE_WEBSEARCH_GATEWAY_NAME} ({gateway_id})"
                )
                break

        if not gateway_id:
            logger.info(
                f"  AgentCore gateway not found: {AGENTCORE_WEBSEARCH_GATEWAY_NAME}"
            )
            return True

        for target in _list_all_agentcore_gateway_targets(gateway_id):
            target_id = target.get("targetId")
            target_name = target.get("name", target_id)
            try:
                agentcore_control_client.delete_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                )
                logger.info(f"  ✓ Deleted gateway target: {target_name} ({target_id})")
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.warning(
                        f"  Could not delete gateway target {target_name}: {e}"
                    )

        # Allow target deletion to propagate before removing the gateway.
        for _ in range(18):
            remaining_targets = _list_all_agentcore_gateway_targets(gateway_id)
            if not remaining_targets:
                break
            logger.info(
                f"  Waiting for {len(remaining_targets)} gateway target(s) to be deleted..."
            )
            time.sleep(10)

        agentcore_control_client.delete_gateway(gatewayIdentifier=gateway_id)
        logger.info(f"  ✓ Deleted gateway: {gateway_id}")
        logger.info("✓ AgentCore Web Search gateway deleted")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(
                f"  AgentCore gateway already deleted: {AGENTCORE_WEBSEARCH_GATEWAY_NAME}"
            )
            return True
        logger.warning(f"  Could not delete AgentCore Web Search gateway: {e}")
        return False
    except Exception as e:
        logger.error(f"Error deleting AgentCore Web Search gateway: {e}")
        return False


def delete_secrets():
    """Delete legacy Secrets Manager secrets if they still exist."""
    logger.info("[4/6] Deleting secrets")

    secret_names = [
        f"openweathermap-{project_name}",
        f"tavilyapikey-{project_name}",
        f"notionapikey-{project_name}",
        f"telegramapikey-{project_name}",
        f"discordapikey-{project_name}",
        f"slackapikey-{project_name}",
    ]

    for secret_name in secret_names:
        try:
            secrets_client.delete_secret(
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True,
            )
            logger.info(f"  ✓ Deleted secret: {secret_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                logger.warning(f"  Could not delete secret {secret_name}: {e}")

    logger.info("✓ Secrets deleted")


def delete_iam_roles(
    delete_agentcore_gateway_role: bool = True,
    delete_knowledge_base_role: bool = False,
):
    """Delete IAM roles created by installer."""
    logger.info("[5/6] Deleting IAM roles")

    role_names = []
    if delete_knowledge_base_role:
        role_names.append(knowledge_base_role_name)
    else:
        logger.info(f"  Keeping shared Knowledge Base IAM role ({knowledge_base_role_name})")

    role_names.extend([
        f"role-agent-for-{project_name}-{region}",
        f"role-agentcore-memory-for-{project_name}-{region}",
    ])

    if delete_agentcore_gateway_role:
        role_names.append(f"role-agentcore-gateway-websearch-for-{project_name}")
    else:
        logger.info(
            "  Keeping AgentCore gateway IAM role "
            f"(role-agentcore-gateway-websearch-for-{project_name})"
        )

    for role_name in role_names:
        try:
            attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
            for policy in attached_policies["AttachedPolicies"]:
                iam_client.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy["PolicyArn"],
                )

            inline_policies = iam_client.list_role_policies(RoleName=role_name)
            for policy_name in inline_policies["PolicyNames"]:
                iam_client.delete_role_policy(
                    RoleName=role_name,
                    PolicyName=policy_name,
                )

            iam_client.delete_role(RoleName=role_name)
            logger.info(f"  ✓ Deleted role: {role_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                logger.warning(f"  Could not delete role {role_name}: {e}")

    logger.info("✓ IAM roles deleted")


def clear_config_json(
    delete_s3_bucket: bool = False,
    delete_cloudfront: bool = False,
    delete_neptune: bool = False,
    delete_knowledge_base: bool = False,
):
    """Remove installer-managed fields from application/config.json."""
    config_path = "application/config.json"
    installer_fields = [
        "agentcore_memory_role",
        "memory_id",
        "agentcore_websearch_gateway_name",
        "agentcore_websearch_gateway_region",
        "agentcore_websearch_gateway_id",
        "agentcore_websearch_gateway_url",
        "agentcore_websearch_gateway_role",
    ]
    if delete_knowledge_base:
        installer_fields.extend(["knowledge_base_id", "knowledge_base_role"])
    if delete_neptune:
        installer_fields.extend([
            "neptune_graph_id",
            "neptune_graph_arn",
            "neptune_graph_name",
            # legacy OpenSearch keys (in case of partial migration)
            "collectionArn",
            "opensearch_url",
        ])
    if delete_s3_bucket:
        installer_fields.extend(["s3_bucket", "s3_arn"])
    if delete_cloudfront:
        installer_fields.append("sharing_url")

    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logger.debug(f"  {config_path} not found, skipping")
        return
    except Exception as e:
        logger.warning(f"  Could not read {config_path}: {e}")
        return

    for field in installer_fields:
        config_data.pop(field, None)

    try:
        with open(config_path, "w") as f:
            json.dump(config_data, f, indent=2)
        logger.info(f"✓ Cleared installer fields from {config_path}")
    except Exception as e:
        logger.warning(f"  Could not update {config_path}: {e}")


def main():
    """Delete all infrastructure created by installer.py."""
    logger.info("=" * 60)
    logger.info("Starting AWS Infrastructure Cleanup")
    logger.info("=" * 60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"S3 Bucket: {bucket_name}")
    logger.info(f"Neptune graph: {neptune_graph_name}")
    logger.info(f"Knowledge Base: {knowledge_base_name}")
    logger.info("=" * 60)

    parser = argparse.ArgumentParser(
        description="Delete AWS infrastructure created by installer.py"
    )
    parser.add_argument("--yes", action="store_true", help="Skip project-specific confirmation")
    parser.add_argument("--delete-agentcore-gateway", action="store_true")
    parser.add_argument("--delete-s3-bucket", action="store_true")
    parser.add_argument("--delete-cloudfront", action="store_true")
    parser.add_argument("--delete-neptune", action="store_true")
    parser.add_argument("--delete-opensearch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--delete-knowledge-base", action="store_true")
    args = parser.parse_args()

    if not args.yes:
        print("\n" + "=" * 60)
        print("WARNING: This will delete project-specific resources")
        print("=" * 60)
        print(f"  Project:  {project_name}")
        print(f"  Region:   {region}")
        print("  Always removed: Secrets, project IAM roles (agent, agentcore memory)")
        print("  AgentCore gateway: prompted separately (default: keep)")
        print("=" * 60)
        print("Shared resources (prompted separately, default: keep):")
        print(f"  S3 bucket:           {bucket_name}")
        print(f"  CloudFront:          {cloudfront_comment}")
        print(f"  Neptune graph:       {neptune_graph_name}")
        print(f"  Knowledge Base:      {knowledge_base_name}")
        print(f"  AgentCore gateway:   {AGENTCORE_WEBSEARCH_GATEWAY_NAME} ({AGENTCORE_GATEWAY_REGION})")
        print("=" * 60)
        print("NOTE: Delete Knowledge Base before Neptune graph to avoid orphan billing.")
        print("=" * 60)
        response = input("\nProceed with project-specific resource deletion? (yes/no): ")
        if response.lower() != "yes":
            print("Uninstallation cancelled.")
            sys.exit(0)

    def resolve(flag, prompt, default=False):
        if flag:
            return True
        if args.yes:
            return False
        return prompt_yes_no(prompt, default=default)

    delete_s3_bucket = resolve(
        args.delete_s3_bucket, f"\nDelete shared S3 bucket ({bucket_name})?"
    )
    delete_cloudfront = resolve(
        args.delete_cloudfront,
        f"Delete shared CloudFront distribution ({cloudfront_comment})?",
    )
    delete_neptune = resolve(
        args.delete_neptune or args.delete_opensearch,
        f"Delete Neptune Analytics graph ({neptune_graph_name})?",
    )
    delete_knowledge_base = resolve(
        args.delete_knowledge_base, f"Delete shared Knowledge Base ({knowledge_base_name})?"
    )
    delete_agentcore_gateway = resolve(
        args.delete_agentcore_gateway,
        f"Delete shared AgentCore Web Search gateway ({AGENTCORE_WEBSEARCH_GATEWAY_NAME})?",
    )

    start_time = time.time()
    try:
        if delete_knowledge_base:
            delete_knowledge_bases()
        else:
            logger.info(f"[skip] Knowledge Base retained (shared resource): {knowledge_base_name}")

        if delete_neptune:
            if not delete_knowledge_base:
                logger.warning(
                    "Deleting Neptune while keeping Knowledge Base may leave the KB broken. "
                    "Prefer deleting the Knowledge Base first."
                )
            delete_neptune_analytics_graph()
        else:
            logger.info(f"[skip] Neptune graph retained (shared resource): {neptune_graph_name}")

        agentcore_gateway_deleted = delete_agentcore_websearch_gateway(
            confirmed=delete_agentcore_gateway
        )
        delete_secrets()
        delete_iam_roles(
            delete_agentcore_gateway_role=agentcore_gateway_deleted,
            delete_knowledge_base_role=delete_knowledge_base,
        )
        clear_config_json(
            delete_s3_bucket=delete_s3_bucket,
            delete_cloudfront=delete_cloudfront,
            delete_neptune=delete_neptune,
            delete_knowledge_base=delete_knowledge_base,
        )

        if delete_s3_bucket:
            delete_s3_buckets()
        else:
            logger.info(f"[skip] S3 bucket retained (shared resource): {bucket_name}")

        if delete_cloudfront:
            disable_cloudfront_distributions()
            wait_for_cloudfront_disabled()
            delete_cloudfront_distributions()
            delete_cloudfront_oai()
        else:
            logger.info(f"[skip] CloudFront retained (shared resource): {cloudfront_comment}")

        elapsed_time = time.time() - start_time
        logger.info("=" * 60)
        logger.info("Infrastructure Cleanup Completed Successfully!")
        logger.info(f"Total cleanup time: {elapsed_time / 60:.2f} minutes")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"Cleanup Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
