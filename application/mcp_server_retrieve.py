import logging
import sys
import mcp_retrieve

from mcp.server.mcpserver import MCPServer 

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("retrieve-server")

try:
    mcp = MCPServer(
        name = "mcp-retrieve"
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
        err_msg = f"Error: {str(e)}"
        logger.info(f"{err_msg}")

######################################
# GraphRAG (Neptune Analytics)
######################################
@mcp.tool()
def retrieve(keyword: str) -> str:
    """
    Query the knowledge base with GraphRAG (Neptune Analytics).
    Uses vector search plus graph traversal over extracted entities/relations.
    Results are scoped to the current user's uploaded documents only
    (metadata owner equals filter).
    keyword: the keyword or natural-language query
    return: JSON documents with contents and reference metadata
    """
    logger.info(f"GraphRAG search --> keyword: {keyword}")

    return mcp_retrieve.retrieve(keyword)

if __name__ =="__main__":
    mcp.run(transport="stdio")
