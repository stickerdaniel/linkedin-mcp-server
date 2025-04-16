# src/linkedin_mcp_server/tools/connections.py
"""
Connection management tools for LinkedIn MCP server.

This module provides tools for managing LinkedIn connections and invitations.
"""

from typing import Dict, Any, List
import logging
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager

logger = logging.getLogger(__name__)


def register_connection_tools(mcp: FastMCP) -> None:
    """
    Register all connection-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def add_connection(profile_url: str, message: str = "") -> Dict[str, Any]:
        """
        Send a connection request to a LinkedIn user.

        Args:
            profile_url (str): LinkedIn profile URL of the person to connect with
            message (str, optional): Message to include with the connection request

        Returns:
            Dict[str, Any]: Result of the connection request
        """
        try:
            client = LinkedInClientManager.get_client()

            # Extract profile ID from URL
            if "/in/" in profile_url:
                profile_id = profile_url.split("/in/")[1].split("/")[0]
            else:
                profile_id = profile_url  # Assume it's already a profile ID

            print(f"🤝 Sending connection request to: {profile_id}")

            # Limit message to 300 characters (LinkedIn limit)
            if message and len(message) > 300:
                message = message[:297] + "..."

            # Add connection
            result = client.add_connection(profile_id, message=message)
            success = not result  # add_connection returns False if successful

            return {
                "success": success,
                "profile_id": profile_id,
                "error": None if success else "Failed to send connection request",
            }
        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            return {
                "success": False,
                "error": f"Failed to send connection request: {str(e)}",
            }

    @mcp.tool()
    async def get_connection_invitations(limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get pending connection invitations.

        Args:
            limit (int, optional): Maximum number of invitations to retrieve

        Returns:
            List[Dict[str, Any]]: List of pending connection invitations
        """
        try:
            client = LinkedInClientManager.get_client()

            print("📨 Retrieving connection invitations")

            # Get invitations
            invitations = client.get_invitations(limit=limit)
            return invitations
        except Exception as e:
            logger.error(f"Error retrieving invitations: {e}")
            return [{"error": f"Failed to retrieve invitations: {str(e)}"}]

    @mcp.tool()
    async def respond_to_invitation(
        invitation_id: str, shared_secret: str, accept: bool = True
    ) -> Dict[str, Any]:
        """
        Respond to a connection invitation.

        Args:
            invitation_id (str): ID of the invitation to respond to
            shared_secret (str): Shared secret of the invitation
            accept (bool, optional): Whether to accept the invitation

        Returns:
            Dict[str, Any]: Result of the response
        """
        try:
            client = LinkedInClientManager.get_client()

            action = "accept" if accept else "reject"
            print(f"✉️ {action.capitalize()}ing invitation: {invitation_id}")

            # Respond to invitation
            result = client.reply_invitation(
                invitation_entity_urn=invitation_id,
                invitation_shared_secret=shared_secret,
                action=action,
            )

            return {
                "success": result,
                "invitation_id": invitation_id,
                "action": action,
                "error": None if result else f"Failed to {action} invitation",
            }
        except Exception as e:
            logger.error(f"Error responding to invitation: {e}")
            return {
                "success": False,
                "error": f"Failed to respond to invitation: {str(e)}",
            }

    @mcp.tool()
    async def remove_connection(profile_url: str) -> Dict[str, Any]:
        """
        Remove a connection with a LinkedIn user.

        Args:
            profile_url (str): LinkedIn profile URL of the connection to remove

        Returns:
            Dict[str, Any]: Result of removing the connection
        """
        try:
            client = LinkedInClientManager.get_client()

            # Extract profile ID from URL
            if "/in/" in profile_url:
                profile_id = profile_url.split("/in/")[1].split("/")[0]
            else:
                profile_id = profile_url  # Assume it's already a profile ID

            print(f"❌ Removing connection: {profile_id}")

            # Remove connection
            result = client.remove_connection(profile_id)
            success = not result  # remove_connection returns False if successful

            return {
                "success": success,
                "profile_id": profile_id,
                "error": None if success else "Failed to remove connection",
            }
        except Exception as e:
            logger.error(f"Error removing connection: {e}")
            return {"success": False, "error": f"Failed to remove connection: {str(e)}"}
