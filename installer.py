#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script creates AWS infrastructure resources for local development.
"""

import boto3
import json
import time
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional
from botocore.exceptions import ClientError

# Configuration
project_name = "rag-graph"  # at least 3 characters
region = "us-west-2"
AGENTCORE_GATEWAY_REGION = "us-east-1"
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
AGENTCORE_WEBSEARCH_TARGET_NAME = "websearch"
vector_index_name = project_name
neptune_graph_name = project_name
neptune_provisioned_memory = 32  # m-NCU (POC). Min 16; 32 recommended for GraphRAG POC
embedding_dimensions = 1024
# Shared with agent-skills / other rag-project apps
SHARED_RAG_PROJECT = "rag-project"
cloudfront_comment = f"CloudFront-for-{SHARED_RAG_PROJECT}"
oai_comment = f"OAI for {SHARED_RAG_PROJECT}"

sts_client = boto3.client("sts", region_name=region)
account_id = sts_client.get_caller_identity()["Account"]

knowledge_base_name = project_name
knowledge_base_role_name = f"role-knowledge-base-for-{project_name}-{region}"

s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
neptune_graph_client = boto3.client("neptune-graph", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=AGENTCORE_GATEWAY_REGION,
)

bucket_name = f"storage-for-{SHARED_RAG_PROJECT}-{account_id}-{region}"

def setup_logging(log_level=logging.INFO):
    """Setup logging configuration."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(),
            # logging.FileHandler(f"installer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        ]
    )
    
    return logging.getLogger(__name__)


logger = setup_logging()


def create_s3_bucket() -> str:
    """Create or reuse shared S3 bucket (storage-for-rag-project-...)."""
    logger.info(f"[1/5] Creating/reusing shared S3 bucket: {bucket_name}")
    
    try:
        # Create bucket
        logger.debug(f"Creating bucket in region: {region}")
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        logger.debug("Bucket created successfully")
        
        # Configure bucket
        logger.debug("Configuring public access block")
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True
            }
        )
        
        # Set CORS configuration
        logger.debug("Setting CORS configuration")
        cors_configuration = {
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "POST", "PUT"],
                    "AllowedOrigins": ["*"]
                }
            ]
        }
        s3_client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_configuration
        )
        
        # Enable versioning (set to false means suspend)
        logger.debug("Configuring versioning")
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Suspended"}
        )
        
        # Create docs/{projectName} and artifacts folders
        logger.debug("Creating docs/%s and artifacts folders", project_name)
        for folder in [f"docs/{project_name}/", "artifacts/"]:
            try:
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=folder,
                    Body=b""
                )
                logger.debug(f"{folder} folder created successfully")
            except ClientError as e:
                logger.warning(f"Failed to create {folder} folder: {e}")
        
        logger.info(f"✓ S3 bucket created successfully: {bucket_name}")
        return bucket_name
    
    except ClientError as e:
        if e.response["Error"]["Code"] in ["BucketAlreadyExists", "BucketAlreadyOwnedByYou"]:
            logger.warning(f"S3 bucket already exists: {bucket_name}")
            # Create docs/{projectName} and artifacts folders if bucket already exists
            logger.debug("Creating docs/%s and artifacts folders in existing bucket", project_name)
            for folder in [f"docs/{project_name}/", "artifacts/"]:
                try:
                    s3_client.put_object(
                        Bucket=bucket_name,
                        Key=folder,
                        Body=b""
                    )
                    logger.debug(f"{folder} folder created successfully")
                except ClientError as folder_error:
                    if folder_error.response["Error"]["Code"] != "NoSuchBucket":
                        logger.warning(f"Failed to create {folder} folder: {folder_error}")
            return bucket_name
        logger.error(f"Failed to create S3 bucket: {e}")
        raise


def create_iam_role(role_name: str, assume_role_policy: Dict, managed_policies: Optional[List[str]] = None) -> str:
    """Create IAM role."""
    logger.debug(f"Creating IAM role: {role_name}")
    
    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=f"Role for {role_name}"
        )
        role_arn = response["Role"]["Arn"]
        logger.debug(f"Role created: {role_arn}")
        
        if managed_policies:
            logger.debug(f"Attaching {len(managed_policies)} managed policies")
            for policy_arn in managed_policies:
                iam_client.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn
                )
                logger.debug(f"Attached policy: {policy_arn}")
        
        logger.info(f"✓ IAM role created: {role_name}")
        return role_arn
    
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.warning(f"IAM role already exists: {role_name}")
            response = iam_client.get_role(RoleName=role_name)
            role_arn = response["Role"]["Arn"]
            
            # Update trust policy for existing role
            try:
                logger.info(f"Updating trust policy for existing role: {role_name}")
                iam_client.update_assume_role_policy(
                    RoleName=role_name,
                    PolicyDocument=json.dumps(assume_role_policy)
                )
                logger.info(f"✓ Updated trust policy for role: {role_name}")
                
                # Verify trust policy was updated correctly
                updated_role = iam_client.get_role(RoleName=role_name)
                policy_doc = updated_role["Role"]["AssumeRolePolicyDocument"]
                # Handle both string and dict formats (boto3 may return either)
                if isinstance(policy_doc, str):
                    updated_policy = json.loads(policy_doc)
                else:
                    updated_policy = policy_doc
                logger.debug(f"Verified trust policy: {json.dumps(updated_policy, indent=2)}")
            except ClientError as trust_policy_error:
                logger.error(f"✗ Failed to update trust policy for role {role_name}: {trust_policy_error}")
                logger.error(f"  Error Code: {trust_policy_error.response.get('Error', {}).get('Code')}")
                logger.error(f"  Error Message: {trust_policy_error.response.get('Error', {}).get('Message')}")
                raise
            
            # Update managed policies if provided
            if managed_policies:
                logger.debug(f"Updating managed policies for existing role")
                # Get currently attached managed policies
                try:
                    attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
                    current_policy_arns = {policy["PolicyArn"] for policy in attached_policies["AttachedPolicies"]}
                    
                    # Attach missing policies
                    for policy_arn in managed_policies:
                        if policy_arn not in current_policy_arns:
                            iam_client.attach_role_policy(
                                RoleName=role_name,
                                PolicyArn=policy_arn
                            )
                            logger.debug(f"Attached missing policy: {policy_arn}")
                except ClientError as policy_error:
                    logger.warning(f"Could not update managed policies: {policy_error}")
            
            return role_arn
        logger.error(f"Failed to create IAM role {role_name}: {e}")
        raise


def attach_inline_policy(role_name: str, policy_name: str, policy_document: Dict):
    """Attach or update inline policy to IAM role."""
    logger.debug(f"Attaching/updating inline policy {policy_name} to {role_name}")
    
    try:
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document)
        )
        logger.debug(f"Policy {policy_name} attached/updated successfully")
    except ClientError as e:
        logger.error(f"Error attaching/updating policy {policy_name}: {e}")
        raise


def create_knowledge_base_role() -> str:
    """Create Knowledge Base IAM role."""
    logger.info("[2/5] Creating Knowledge Base IAM role")
    role_name = knowledge_base_role_name
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    role_arn = create_iam_role(role_name, assume_role_policy)
    
    # Always attach/update inline policies (put_role_policy will create or update)
    bedrock_invoke_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:*",
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel"
                ],
                "Resource": [
                    "*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:*:inference-profile/*",
                    "arn:aws:bedrock:*::foundation-model/*"
                ]
            }
        ]
    }
    attach_inline_policy(role_name, f"bedrock-invoke-policy-for-{vector_index_name}", bedrock_invoke_policy)
    
    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:*"],
                "Resource": ["*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"knowledge-base-s3-policy-for-{vector_index_name}", s3_policy)
    
    neptune_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "NeptuneAnalyticsAccess",
                "Effect": "Allow",
                "Action": [
                    "neptune-graph:GetGraph",
                    "neptune-graph:ReadDataViaQuery",
                    "neptune-graph:WriteDataViaQuery",
                    "neptune-graph:DeleteDataViaQuery",
                ],
                "Resource": [f"arn:aws:neptune-graph:{region}:{account_id}:graph/*"],
            }
        ],
    }
    attach_inline_policy(
        role_name,
        f"bedrock-agent-neptune-policy-for-{vector_index_name}",
        neptune_policy,
    )
    
    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:*",
                    "bedrock:GetInferenceProfile"
                ],
                "Resource": [
                    "*",
                    f"arn:aws:bedrock:{region}:*:inference-profile/*"
                ]
            }
        ]
    }
    attach_inline_policy(role_name, f"bedrock-agent-bedrock-policy-for-{vector_index_name}", bedrock_policy)
    
    return role_arn


def create_agent_role() -> str:
    """Create Agent IAM role."""
    logger.info("[2/5] Creating Agent IAM role")
    role_name = f"role-agent-for-{project_name}-{region}"
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    role_arn = create_iam_role(role_name, assume_role_policy, ["arn:aws:iam::aws:policy/AWSLambdaExecute"])
    
    # Always attach/update inline policies
    bedrock_retrieve_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock:Retrieve"],
                "Resource": [f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"bedrock-retrieve-policy-for-{project_name}", bedrock_retrieve_policy)
    
    inference_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel"
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    "arn:aws:bedrock:*::foundation-model/*"
                ]
            }
        ]
    }
    attach_inline_policy(role_name, f"agent-inference-policy-for-{project_name}", inference_policy)
    
    lambda_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction", "cloudwatch:*"],
                "Resource": ["*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"lambda-invoke-policy-for-{project_name}", lambda_policy)
    
    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock:*"],
                "Resource": ["*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"bedrock-policy-agent-for-{project_name}", bedrock_policy)
    
    return role_arn


def create_neptune_analytics_graph() -> Dict[str, str]:
    """Create Neptune Analytics graph with vector search index for GraphRAG."""
    logger.info("[3/5] Creating Neptune Analytics graph")

    # Reuse existing graph if present
    try:
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = neptune_graph_client.list_graphs(**kwargs)
            for graph in response.get("graphs", []):
                if graph.get("name") != neptune_graph_name:
                    continue
                graph_id = graph["id"]
                detail = neptune_graph_client.get_graph(graphIdentifier=graph_id)
                status = detail.get("status")
                logger.warning(
                    f"Neptune graph already exists: {neptune_graph_name} ({graph_id}), status={status}"
                )
                if status in ("CREATING", "UPDATING", "STARTING"):
                    logger.info("  Waiting for existing Neptune graph to become AVAILABLE...")
                    while True:
                        detail = neptune_graph_client.get_graph(graphIdentifier=graph_id)
                        status = detail.get("status")
                        if status == "AVAILABLE":
                            break
                        if status in ("FAILED", "DELETING", "DELETED"):
                            raise Exception(f"Neptune graph entered bad status: {status}")
                        time.sleep(15)
                elif status == "STOPPED":
                    logger.info("  Starting stopped Neptune graph...")
                    neptune_graph_client.start_graph(graphIdentifier=graph_id)
                    while True:
                        detail = neptune_graph_client.get_graph(graphIdentifier=graph_id)
                        status = detail.get("status")
                        if status == "AVAILABLE":
                            break
                        if status in ("FAILED", "DELETING"):
                            raise Exception(f"Failed to start Neptune graph: {status}")
                        time.sleep(15)
                elif status != "AVAILABLE":
                    raise Exception(f"Neptune graph is not usable (status={status})")

                vector_cfg = detail.get("vectorSearchConfiguration") or {}
                dimension = vector_cfg.get("dimension")
                if dimension and dimension != embedding_dimensions:
                    raise Exception(
                        f"Existing Neptune graph vector dimension is {dimension}, "
                        f"expected {embedding_dimensions}. Delete the graph or recreate it."
                    )

                logger.info(f"✓ Reusing Neptune graph: {detail['arn']}")
                return {
                    "id": graph_id,
                    "arn": detail["arn"],
                    "name": detail.get("name", neptune_graph_name),
                    "endpoint": detail.get("endpoint", ""),
                }
            next_token = response.get("nextToken")
            if not next_token:
                break
    except Exception as e:
        if "expected" in str(e) or "not usable" in str(e) or "bad status" in str(e):
            raise
        logger.debug(f"Error checking existing Neptune graphs: {e}")

    logger.info(
        f"  Creating Neptune graph '{neptune_graph_name}' "
        f"({neptune_provisioned_memory} m-NCU, vector dim={embedding_dimensions})..."
    )
    response = neptune_graph_client.create_graph(
        graphName=neptune_graph_name,
        provisionedMemory=neptune_provisioned_memory,
        publicConnectivity=False,
        deletionProtection=False,
        vectorSearchConfiguration={"dimension": embedding_dimensions},
        tags={
            "project": project_name,
            "purpose": "graphrag",
        },
    )
    graph_id = response["id"]
    graph_arn = response["arn"]
    logger.info(f"  Neptune graph create requested: {graph_id}")

    logger.info("  Waiting for Neptune graph to become AVAILABLE (may take several minutes)...")
    while True:
        detail = neptune_graph_client.get_graph(graphIdentifier=graph_id)
        status = detail.get("status")
        if status == "AVAILABLE":
            logger.info(f"✓ Neptune graph AVAILABLE: {graph_arn}")
            return {
                "id": graph_id,
                "arn": detail.get("arn", graph_arn),
                "name": detail.get("name", neptune_graph_name),
                "endpoint": detail.get("endpoint", ""),
            }
        if status in ("FAILED", "DELETING"):
            reason = detail.get("statusReason", "")
            raise Exception(f"Neptune graph creation failed: {status} {reason}")
        logger.debug(f"  Neptune graph status: {status}")
        time.sleep(15)


def delete_knowledge_base(knowledge_base_id: str) -> None:
    """Delete Knowledge Base and its data sources."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

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
                    logger.debug(f"Deleted data source: {ds['dataSourceId']}")
                except Exception as e:
                    logger.warning(f"Failed to delete data source {ds['dataSourceId']}: {e}")
        except Exception as e:
            logger.debug(f"Error listing/deleting data sources: {e}")

        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"Deleted Knowledge Base: {knowledge_base_id}")

        logger.debug("Waiting for Knowledge Base deletion to complete...")
        max_wait = 60
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
                    logger.debug("Knowledge Base deletion confirmed")
                    break
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.debug(f"Knowledge Base {knowledge_base_id} already deleted")
        else:
            logger.error(f"Failed to delete Knowledge Base {knowledge_base_id}: {e}")
            raise


def _verify_knowledge_base_role() -> None:
    """Ensure Knowledge Base role trust policy allows bedrock.amazonaws.com."""
    logger.info("  Verifying Knowledge Base role configuration...")
    role_response = iam_client.get_role(RoleName=knowledge_base_role_name)
    policy_doc = role_response["Role"]["AssumeRolePolicyDocument"]
    if isinstance(policy_doc, str):
        trust_policy = json.loads(policy_doc)
    else:
        trust_policy = policy_doc

    bedrock_allowed = False
    for statement in trust_policy.get("Statement", []):
        if statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal", {})
        if principal.get("Service") == "bedrock.amazonaws.com":
            bedrock_allowed = True
            break

    if not bedrock_allowed:
        raise Exception("Knowledge Base role trust policy does not allow bedrock.amazonaws.com")
    logger.info("  ✓ Knowledge Base role trust policy is correct")


# GraphRAG ingestion models (inference profiles in account region)
PARSER_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
GRAPH_CONSTRUCTION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _inference_profile_arn(model_id: str) -> str:
    return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{model_id}"


def _graphrag_vector_ingestion_configuration(
    *,
    parser_model_arn: Optional[str] = None,
    graph_construction_model_arn: Optional[str] = None,
) -> Dict:
    """Neptune Analytics GraphRAG ingestion: FM parser + entity extraction."""
    parser_arn = parser_model_arn or _inference_profile_arn(PARSER_MODEL_ID)
    graph_arn = graph_construction_model_arn or _inference_profile_arn(
        GRAPH_CONSTRUCTION_MODEL_ID
    )
    return {
        "chunkingConfiguration": {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {
                "maxTokens": 300,
                "overlapPercentage": 20,
            },
        },
        # Foundation model as parser (Claude Sonnet 4.6)
        "parsingConfiguration": {
            "parsingStrategy": "BEDROCK_FOUNDATION_MODEL",
            "bedrockFoundationModelConfiguration": {
                "modelArn": parser_arn,
            },
        },
        # Model for graph construction (Claude Haiku 4.5)
        "contextEnrichmentConfiguration": {
            "type": "BEDROCK_FOUNDATION_MODEL",
            "bedrockFoundationModelConfiguration": {
                "modelArn": graph_arn,
                "enrichmentStrategyConfiguration": {
                    "method": "CHUNK_ENTITY_EXTRACTION",
                },
            },
        },
    }


def _data_source_has_expected_ingestion(details: Dict) -> bool:
    """True when DS already uses Sonnet 4.6 parser + Haiku 4.5 graph construction."""
    vic = details.get("dataSource", {}).get("vectorIngestionConfiguration") or {}
    parsing = vic.get("parsingConfiguration") or {}
    if parsing.get("parsingStrategy") != "BEDROCK_FOUNDATION_MODEL":
        return False
    parser_arn = (
        (parsing.get("bedrockFoundationModelConfiguration") or {}).get("modelArn") or ""
    )
    if PARSER_MODEL_ID not in parser_arn:
        return False
    enrichment = vic.get("contextEnrichmentConfiguration") or {}
    graph_arn = (
        (enrichment.get("bedrockFoundationModelConfiguration") or {}).get("modelArn")
        or ""
    )
    return GRAPH_CONSTRUCTION_MODEL_ID in graph_arn


def _ensure_graphrag_data_source(
    bedrock_agent_client,
    knowledge_base_id: str,
    s3_bucket_name: str,
) -> Optional[str]:
    """Ensure KB data source points at the shared S3 bucket and docs/{project}/ prefix.

    Returns the usable data source id, or None if none could be ensured.
    Parsing strategy cannot be changed after DS creation, so a mismatched
    parser/graph model causes a new data source to be created and the old one deleted.
    """
    expected_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
    expected_prefix = f"docs/{project_name}/"
    vector_ingestion = _graphrag_vector_ingestion_configuration()

    try:
        data_sources = bedrock_agent_client.list_data_sources(
            knowledgeBaseId=knowledge_base_id,
            maxResults=100,
        )
    except Exception as e:
        logger.warning(f"  Could not list data sources for KB {knowledge_base_id}: {e}")
        return None

    matched_id: Optional[str] = None
    stale_ids: List[str] = []

    for ds in data_sources.get("dataSourceSummaries", []):
        ds_id = ds["dataSourceId"]
        try:
            details = bedrock_agent_client.get_data_source(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=ds_id,
            )
            ds_cfg = details["dataSource"].get("dataSourceConfiguration", {})
            s3_cfg = ds_cfg.get("s3Configuration") or {}
            bucket_arn = s3_cfg.get("bucketArn", "")
            prefixes = s3_cfg.get("inclusionPrefixes") or []
            bucket_ok = (
                bucket_arn == expected_bucket_arn and expected_prefix in prefixes
            )
            ingestion_ok = _data_source_has_expected_ingestion(details)

            if bucket_ok and ingestion_ok:
                logger.info(
                    f"  ✓ Data source ready "
                    f"(parser={PARSER_MODEL_ID}, graph={GRAPH_CONSTRUCTION_MODEL_ID}): "
                    f"{ds_id}"
                )
                matched_id = ds_id
                continue

            if not bucket_ok:
                logger.warning(
                    f"  Updating data source {ds_id} to shared S3 "
                    f"({s3_bucket_name}/{expected_prefix})"
                )
                try:
                    bedrock_agent_client.update_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds_id,
                        name=s3_bucket_name,
                        description=f"S3 data source for GraphRAG: {s3_bucket_name}",
                        dataSourceConfiguration={
                            "type": "S3",
                            "s3Configuration": {
                                "bucketArn": expected_bucket_arn,
                                "inclusionPrefixes": [expected_prefix],
                            },
                        },
                        vectorIngestionConfiguration=vector_ingestion,
                    )
                    # Re-check: parsing strategy may be immutable → still stale
                    refreshed = bedrock_agent_client.get_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds_id,
                    )
                    if _data_source_has_expected_ingestion(refreshed) and (
                        (refreshed["dataSource"]
                         .get("dataSourceConfiguration", {})
                         .get("s3Configuration", {})
                         .get("bucketArn")
                         == expected_bucket_arn)
                    ):
                        logger.info(f"  ✓ Data source updated: {ds_id}")
                        matched_id = ds_id
                        continue
                except Exception as update_err:
                    logger.warning(
                        f"  Could not update data source {ds_id}: {update_err}"
                    )

            if bucket_ok and not ingestion_ok:
                logger.warning(
                    f"  Data source {ds_id} has wrong parser/graph model "
                    f"(parsing strategy is immutable); will recreate"
                )
            stale_ids.append(ds_id)
        except Exception as e:
            logger.warning(f"  Could not inspect/update data source {ds_id}: {e}")
            stale_ids.append(ds_id)

    if not matched_id:
        logger.info(
            "  Creating GraphRAG data source "
            f"(parser={PARSER_MODEL_ID}, graph={GRAPH_CONSTRUCTION_MODEL_ID})..."
        )
        data_source_response = bedrock_agent_client.create_data_source(
            knowledgeBaseId=knowledge_base_id,
            name=s3_bucket_name,
            description=f"S3 data source for GraphRAG: {s3_bucket_name}",
            dataDeletionPolicy="RETAIN",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": expected_bucket_arn,
                    "inclusionPrefixes": [expected_prefix],
                },
            },
            vectorIngestionConfiguration=vector_ingestion,
        )
        matched_id = data_source_response["dataSource"]["dataSourceId"]
        logger.info(f"  ✓ Data source created: {matched_id}")

    for stale_id in stale_ids:
        if stale_id == matched_id:
            continue
        try:
            bedrock_agent_client.delete_data_source(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=stale_id,
            )
            logger.info(f"  ✓ Deleted stale data source: {stale_id}")
        except Exception as e:
            logger.warning(f"  Could not delete stale data source {stale_id}: {e}")

    return matched_id


def create_knowledge_base_with_neptune(
    neptune_info: Dict[str, str],
    knowledge_base_role_arn: str,
    s3_bucket_name: str,
) -> str:
    """Create Bedrock Knowledge Base backed by Neptune Analytics (GraphRAG)."""
    logger.info("[4/5] Creating Knowledge Base with Neptune Analytics (GraphRAG)")

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    # Reuse existing KB if it already points at this Neptune graph
    try:
        logger.info("  Checking if Knowledge Base already exists...")
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] != knowledge_base_name:
                continue
            logger.warning(f"Knowledge Base already exists: {kb['knowledgeBaseId']}")
            kb_details = bedrock_agent_client.get_knowledge_base(
                knowledgeBaseId=kb["knowledgeBaseId"]
            )
            storage = kb_details["knowledgeBase"].get("storageConfiguration", {})
            storage_type = storage.get("type")
            neptune_cfg = storage.get("neptuneAnalyticsConfiguration") or {}
            current_graph_arn = neptune_cfg.get("graphArn")

            if storage_type == "NEPTUNE_ANALYTICS" and current_graph_arn == neptune_info["arn"]:
                logger.info("Knowledge Base is using the correct Neptune Analytics graph")
                data_source_id = _ensure_graphrag_data_source(
                    bedrock_agent_client, kb["knowledgeBaseId"], s3_bucket_name
                )
                if data_source_id:
                    write_application_config({"data_source_id": data_source_id})
                return kb["knowledgeBaseId"]

            logger.warning("Knowledge Base is not using the expected Neptune graph:")
            logger.warning(f"  storage type: {storage_type}")
            logger.warning(f"  current graph: {current_graph_arn}")
            logger.warning(f"  expected graph: {neptune_info['arn']}")
            delete_knowledge_base(kb["knowledgeBaseId"])
            break
        logger.info("  Knowledge Base does not exist. Creating new one...")
    except Exception as e:
        logger.debug(f"Error checking existing Knowledge Base: {e}")

    _verify_knowledge_base_role()

    # IAM propagation for newly attached Neptune permissions
    logger.info("  Waiting for IAM role policy propagation...")
    time.sleep(10)

    logger.debug(f"Creating Knowledge Base with Neptune graph: {neptune_info['arn']}")
    response = bedrock_agent_client.create_knowledge_base(
        name=knowledge_base_name,
        description="GraphRAG knowledge base based on Neptune Analytics",
        roleArn=knowledge_base_role_arn,
        tags={knowledge_base_name: "true", "vectorStore": "NEPTUNE_ANALYTICS"},
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": (
                    f"arn:aws:bedrock:{region}::foundation-model/"
                    "amazon.titan-embed-text-v2:0"
                ),
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {
                        "dimensions": embedding_dimensions,
                        "embeddingDataType": "FLOAT32",
                    }
                },
            },
        },
        storageConfiguration={
            "type": "NEPTUNE_ANALYTICS",
            "neptuneAnalyticsConfiguration": {
                "graphArn": neptune_info["arn"],
                "fieldMapping": {
                    "textField": "text",
                    "metadataField": "metadata",
                },
            },
        },
    )

    knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]
    logger.info(f"✓ Knowledge Base created: {knowledge_base_id}")

    logger.info("  Waiting for Knowledge Base to be active...")
    while True:
        kb_response = bedrock_agent_client.get_knowledge_base(
            knowledgeBaseId=knowledge_base_id
        )
        status = kb_response["knowledgeBase"]["status"]
        if status == "ACTIVE":
            logger.info("  Knowledge Base is now active")
            break
        if status == "FAILED":
            reasons = kb_response["knowledgeBase"].get("failureReasons", [])
            raise Exception(f"Knowledge Base creation failed: {reasons}")
        logger.debug(f"  Knowledge Base status: {status} (waiting...)")
        time.sleep(10)

    logger.info(
        "  Creating GraphRAG data source "
        f"(parser={PARSER_MODEL_ID}, graph={GRAPH_CONSTRUCTION_MODEL_ID})..."
    )
    data_source_response = bedrock_agent_client.create_data_source(
        knowledgeBaseId=knowledge_base_id,
        name=s3_bucket_name,
        description=f"S3 data source for GraphRAG: {s3_bucket_name}",
        dataDeletionPolicy="RETAIN",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{s3_bucket_name}",
                "inclusionPrefixes": [f"docs/{project_name}/"],
            },
        },
        vectorIngestionConfiguration=_graphrag_vector_ingestion_configuration(),
    )

    data_source_id = data_source_response["dataSource"]["dataSourceId"]
    logger.info(f"  ✓ Data source created: {data_source_id}")
    write_application_config({"data_source_id": data_source_id})
    logger.info(
        "  Note: Run Bedrock Knowledge Base Sync after uploading docs/%s/ to build the graph.",
        project_name,
    )

    return knowledge_base_id


def _agentcore_websearch_tool_arn() -> str:
    return (
        f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
        f"aws:tool/web-search.v1"
    )


def _list_all_agentcore_gateways() -> List[Dict]:
    gateways: List[Dict] = []
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


def _list_all_agentcore_gateway_targets(gateway_id: str) -> List[Dict]:
    targets: List[Dict] = []
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


def wait_for_agentcore_gateway_ready(gateway_id: str, timeout_seconds: int = 600) -> Dict:
    """Wait until an AgentCore gateway reaches READY status."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        gateway = agentcore_control_client.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status", "")
        if status == "READY":
            logger.info(f"  AgentCore gateway is ready: {gateway_id}")
            return gateway
        if status in ("FAILED", "DELETING", "DELETE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL"):
            raise RuntimeError(
                f"AgentCore gateway {gateway_id} entered terminal status: {status}"
            )
        logger.info(f"  Waiting for AgentCore gateway ({gateway_id}) status: {status}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for AgentCore gateway {gateway_id} to become READY")


def create_agentcore_websearch_gateway_role() -> str:
    """Create IAM service role for the AgentCore Web Search gateway."""
    logger.info("[2/5] Creating AgentCore Web Search gateway IAM role")
    role_name = f"role-agentcore-gateway-websearch-for-{project_name}"

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GatewayAssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
                            f"{account_id}:gateway/{AGENTCORE_WEBSEARCH_GATEWAY_NAME}-*"
                        )
                    },
                },
            }
        ],
    }
    role_arn = create_iam_role(role_name, assume_role_policy)

    gateway_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": [
                    (
                        f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
                        f"{account_id}:gateway/*"
                    )
                ],
            },
            {
                "Sid": "InvokeWebSearchTool",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeWebSearch"],
                "Resource": [_agentcore_websearch_tool_arn()],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"agentcore-gateway-websearch-policy-for-{project_name}",
        gateway_policy,
    )
    return role_arn


def _ensure_websearch_gateway_target(gateway_id: str) -> str:
    """Create the managed web-search connector target if it does not exist."""
    for target in _list_all_agentcore_gateway_targets(gateway_id):
        if target.get("name") == AGENTCORE_WEBSEARCH_TARGET_NAME:
            target_id = target["targetId"]
            logger.warning(
                f"  AgentCore websearch target already exists: {target_id}"
            )
            return target_id

    logger.info("  Creating AgentCore websearch gateway target")
    response = agentcore_control_client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=AGENTCORE_WEBSEARCH_TARGET_NAME,
        description=f"Managed Web Search connector for {project_name}",
        targetConfiguration={
            "mcp": {
                "connector": {
                    "source": {
                        "connectorId": "web-search",
                    },
                    "configurations": [
                        {
                            "name": "WebSearch",
                            "parameterValues": {},
                        }
                    ],
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )
    target_id = response["targetId"]
    logger.info(f"  ✓ AgentCore websearch target created: {target_id}")

    try:
        agentcore_control_client.synchronize_gateway_targets(
            gatewayIdentifier=gateway_id,
            targetIdList=[target_id],
        )
    except ClientError as e:
        logger.warning(f"  Could not synchronize gateway target immediately: {e}")

    return target_id


def get_or_create_agentcore_websearch_gateway(gateway_service_role_arn: str) -> Dict[str, str]:
    """Create or reuse shared gateway-websearch with the managed web-search connector."""
    logger.info("[2/5] Creating/reusing shared AgentCore Web Search gateway")

    gateway_id = None
    for gateway in _list_all_agentcore_gateways():
        if gateway.get("name") == AGENTCORE_WEBSEARCH_GATEWAY_NAME:
            gateway_id = gateway["gatewayId"]
            logger.warning(
                f"  AgentCore gateway already exists: "
                f"{AGENTCORE_WEBSEARCH_GATEWAY_NAME} ({gateway_id})"
            )
            break

    if not gateway_id:
        response = agentcore_control_client.create_gateway(
            name=AGENTCORE_WEBSEARCH_GATEWAY_NAME,
            description=f"AgentCore Web Search gateway for {project_name}",
            roleArn=gateway_service_role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            tags={"project": project_name},
        )
        gateway_id = response["gatewayId"]
        logger.info(f"  ✓ AgentCore gateway created: {gateway_id}")
        wait_for_agentcore_gateway_ready(gateway_id)

    gateway = wait_for_agentcore_gateway_ready(gateway_id)
    target_id = _ensure_websearch_gateway_target(gateway_id)
    gateway_url = gateway.get("gatewayUrl", "").rstrip("/")

    return {
        "gateway_id": gateway_id,
        "gateway_name": AGENTCORE_WEBSEARCH_GATEWAY_NAME,
        "gateway_region": AGENTCORE_GATEWAY_REGION,
        "gateway_url": gateway_url,
        "gateway_arn": gateway.get("gatewayArn", ""),
        "gateway_service_role_arn": gateway_service_role_arn,
        "target_id": target_id,
    }


def _apply_websearch_gateway_config(
    env: Dict[str, str],
    agentcore_websearch_gateway_info: Optional[Dict[str, str]] = None,
) -> None:
    """Add AgentCore websearch gateway settings to an environment/config dict."""
    if not agentcore_websearch_gateway_info:
        return
    env["agentcore_websearch_gateway_name"] = agentcore_websearch_gateway_info.get(
        "gateway_name", AGENTCORE_WEBSEARCH_GATEWAY_NAME
    )
    env["agentcore_websearch_gateway_region"] = agentcore_websearch_gateway_info.get(
        "gateway_region", AGENTCORE_GATEWAY_REGION
    )
    env["agentcore_websearch_gateway_id"] = agentcore_websearch_gateway_info.get(
        "gateway_id", ""
    )
    env["agentcore_websearch_gateway_url"] = agentcore_websearch_gateway_info.get(
        "gateway_url", ""
    )
    env["agentcore_websearch_gateway_role"] = agentcore_websearch_gateway_info.get(
        "gateway_service_role_arn", ""
    )


def create_cloudfront_distribution(s3_bucket_name: str) -> Dict[str, str]:
    """Create or reuse shared CloudFront distribution (CloudFront-for-rag-project)."""
    logger.info("[5/5] Creating/reusing shared CloudFront distribution")

    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if cloudfront_comment in dist.get("Comment", ""):
                if dist.get("Enabled", False):
                    logger.warning(f"CloudFront distribution already exists: {dist['DomainName']}")
                    return {"id": dist["Id"], "domain": dist["DomainName"]}
                logger.warning(f"CloudFront distribution exists but is disabled: {dist['DomainName']}")
                dist_config_response = cloudfront_client.get_distribution_config(Id=dist["Id"])
                dist_config = dist_config_response["DistributionConfig"]
                dist_config["Enabled"] = True
                cloudfront_client.update_distribution(
                    Id=dist["Id"],
                    DistributionConfig=dist_config,
                    IfMatch=dist_config_response["ETag"],
                )
                return {"id": dist["Id"], "domain": dist["DomainName"]}
    except Exception as e:
        logger.debug(f"Error checking existing CloudFront distributions: {e}")

    oai_id = None
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get("Items", []):
            if oai_comment in oai.get("Comment", ""):
                oai_id = oai["Id"]
                logger.info(f"  Using existing Origin Access Identity: {oai_id}")
                break
        if not oai_id:
            oai_response = cloudfront_client.create_cloud_front_origin_access_identity(
                CloudFrontOriginAccessIdentityConfig={
                    "CallerReference": f"{SHARED_RAG_PROJECT}-s3-oai-{int(time.time())}",
                    "Comment": oai_comment,
                }
            )
            oai_id = oai_response["CloudFrontOriginAccessIdentity"]["Id"]
            logger.info(f"  Created Origin Access Identity: {oai_id}")
    except ClientError as e:
        logger.error(f"Failed to handle Origin Access Identity: {e}")
        raise

    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontAccess",
                "Effect": "Allow",
                "Principal": {
                    "AWS": f"arn:aws:iam::cloudfront:user/CloudFront Origin Access Identity {oai_id}"
                },
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{s3_bucket_name}/*",
            }
        ],
    }
    try:
        time.sleep(10)
        s3_client.put_bucket_policy(Bucket=s3_bucket_name, Policy=json.dumps(bucket_policy))
        logger.info("  Updated S3 bucket policy for CloudFront access")
    except ClientError as e:
        logger.error(f"Failed to update S3 bucket policy: {e}")
        raise

    origin_id = f"s3-{SHARED_RAG_PROJECT}"
    distribution_config = {
        "CallerReference": f"{project_name}-{int(time.time())}",
        "Comment": cloudfront_comment,
        "DefaultRootObject": "index.html",
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
            "Compress": True,
        },
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": origin_id,
                    "DomainName": f"{s3_bucket_name}.s3.{region}.amazonaws.com",
                    "S3OriginConfig": {
                        "OriginAccessIdentity": f"origin-access-identity/cloudfront/{oai_id}"
                    },
                }
            ],
        },
        "Enabled": True,
        "PriceClass": "PriceClass_200",
    }

    response = cloudfront_client.create_distribution(DistributionConfig=distribution_config)
    distribution_id = response["Distribution"]["Id"]
    distribution_domain = response["Distribution"]["DomainName"]
    logger.info(f"CloudFront distribution created: {distribution_domain}")
    logger.info(f"  S3 origin: {s3_bucket_name}")
    return {"id": distribution_id, "domain": distribution_domain}


def build_app_environment(
    knowledge_base_role_arn: str,
    neptune_info: Dict[str, str],
    s3_bucket_name: str,
    cloudfront_domain: str,
    knowledge_base_id: str,
    agentcore_websearch_gateway_info: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    env = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        "knowledge_base_id": knowledge_base_id,
        "knowledge_base_name": knowledge_base_name,
        "knowledge_base_role": knowledge_base_role_arn,
        "neptune_graph_id": neptune_info["id"],
        "neptune_graph_arn": neptune_info["arn"],
        "neptune_graph_name": neptune_info.get("name", neptune_graph_name),
        "s3_bucket": s3_bucket_name,
        "s3_arn": f"arn:aws:s3:::{s3_bucket_name}",
        "sharing_url": f"https://{cloudfront_domain}",
    }
    _apply_websearch_gateway_config(env, agentcore_websearch_gateway_info)
    return env


def _application_config_path() -> str:
    project_root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(project_root, "application", "config.json")


def write_application_config(config_data: Dict, *, merge_existing: bool = True) -> bool:
    config_path = _application_config_path()
    existing = {}
    if merge_existing:
        try:
            with open(config_path, "r") as f:
                existing = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not read existing {config_path}: {e}")
    existing.update(config_data)
    # Drop legacy OpenSearch keys after GraphRAG / Neptune migration
    for legacy_key in ("collectionArn", "opensearch_url"):
        existing.pop(legacy_key, None)
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Could not write {config_path}: {e}")
        return False


def build_config_from_deployment_state(
    knowledge_base_id: Optional[str] = None,
    knowledge_base_role_arn: Optional[str] = None,
    agentcore_websearch_gateway_info: Optional[Dict[str, str]] = None,
    neptune_info: Optional[Dict[str, str]] = None,
    s3_bucket_name: Optional[str] = None,
    cloudfront_info: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    config_data: Dict[str, str] = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        "knowledge_base_name": knowledge_base_name,
    }
    if knowledge_base_id:
        config_data["knowledge_base_id"] = knowledge_base_id
    if knowledge_base_role_arn:
        config_data["knowledge_base_role"] = knowledge_base_role_arn
    if neptune_info:
        config_data["neptune_graph_id"] = neptune_info.get("id", "")
        config_data["neptune_graph_arn"] = neptune_info.get("arn", "")
        config_data["neptune_graph_name"] = neptune_info.get("name", neptune_graph_name)
    if s3_bucket_name:
        config_data["s3_bucket"] = s3_bucket_name
        config_data["s3_arn"] = f"arn:aws:s3:::{s3_bucket_name}"
    if cloudfront_info:
        config_data["sharing_url"] = f"https://{cloudfront_info.get('domain', '')}"
    _apply_websearch_gateway_config(config_data, agentcore_websearch_gateway_info)
    return config_data


def main():
    logger.info("=" * 60)
    logger.info("Starting AWS Infrastructure Deployment")
    logger.info("=" * 60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Bucket Name: {bucket_name}")
    logger.info("=" * 60)

    start_time = time.time()
    s3_bucket_name = None
    knowledge_base_role_arn = None
    agentcore_websearch_gateway_info = None
    neptune_info = None
    knowledge_base_id = None
    cloudfront_info = None
    app_environment = None
    deployment_success = False

    try:
        s3_bucket_name = create_s3_bucket()
        knowledge_base_role_arn = create_knowledge_base_role()
        create_agent_role()
        agentcore_websearch_gateway_role_arn = create_agentcore_websearch_gateway_role()
        agentcore_websearch_gateway_info = get_or_create_agentcore_websearch_gateway(
            agentcore_websearch_gateway_role_arn
        )
        neptune_info = create_neptune_analytics_graph()
        knowledge_base_id = create_knowledge_base_with_neptune(
            neptune_info, knowledge_base_role_arn, s3_bucket_name
        )
        cloudfront_info = create_cloudfront_distribution(s3_bucket_name)
        app_environment = build_app_environment(
            knowledge_base_role_arn,
            neptune_info,
            s3_bucket_name,
            cloudfront_info["domain"],
            knowledge_base_id,
            agentcore_websearch_gateway_info,
        )
        deployment_success = True

        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("=" * 60)
        logger.info("Infrastructure Deployment Completed Successfully!")
        logger.info("=" * 60)
        logger.info(f"  S3 Bucket: {s3_bucket_name}")
        logger.info(f"  CloudFront Domain: https://{cloudfront_info['domain']}")
        logger.info(f"  Neptune Graph ARN: {neptune_info['arn']}")
        logger.info(f"  Neptune Graph ID: {neptune_info['id']}")
        logger.info(f"  Knowledge Base ID: {knowledge_base_id}")
        logger.info(f"  Knowledge Base Role: {knowledge_base_role_arn}")
        logger.info(f"Total deployment time: {elapsed_time / 60:.2f} minutes")
        logger.info(
            "Upload documents to s3://{}/docs/{}/ then Sync the Knowledge Base.".format(
                s3_bucket_name, project_name
            )
        )
        logger.info("Run locally: ./run_local.sh")
        logger.info("=" * 60)
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Deployment Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise
    finally:
        if app_environment is not None:
            config_data = app_environment
        else:
            config_data = build_config_from_deployment_state(
                knowledge_base_id=knowledge_base_id,
                knowledge_base_role_arn=knowledge_base_role_arn,
                agentcore_websearch_gateway_info=agentcore_websearch_gateway_info,
                neptune_info=neptune_info,
                s3_bucket_name=s3_bucket_name,
                cloudfront_info=cloudfront_info,
            )
        if write_application_config(config_data):
            if deployment_success:
                logger.info(f"Updated {_application_config_path()}")
            else:
                logger.info(f"Saved partial deployment info to {_application_config_path()}")


if __name__ == "__main__":
    main()
