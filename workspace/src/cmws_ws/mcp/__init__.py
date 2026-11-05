"""AgentTrunk as an MCP server: agents in any MCP client share one validated trunk."""

from .service import TrunkError, TrunkService

__all__ = ["TrunkError", "TrunkService"]
