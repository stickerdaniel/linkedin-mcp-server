# src/linkedin_mcp_server/tools/messaging.py
"""
Messaging tools for LinkedIn MCP server.

This module provides tools for sending and reading LinkedIn messages.
"""

from typing import Dict, Any, List, Optional
import logging
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager

logger = logging.getLogger(__name__)


def register_messaging_tools(mcp: FastMCP) -> None:
    """
    Register all messaging-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def send_message(
        message_body: str,
        recipient_url: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a message to a LinkedIn connection or conversation.

        Args:
            message_body (str): Content of the message to send
            recipient_url (str, optional): LinkedIn profile URL of recipient (for new conversations)
            conversation_id (str, optional): ID of existing conversation to continue

        Returns:
            Dict[str, Any]: Result of sending the message
        """
        try:
            client = LinkedInClientManager.get_client()

            # Logic to determine whether to create new conversation or use existing
            if conversation_id:
                print(f"💬 Sending message to conversation: {conversation_id}")
                result = client.send_message(
                    message_body=message_body, conversation_urn_id=conversation_id
                )
                success = not result  # send_message returns False if successful

                return {
                    "success": success,
                    "conversation_id": conversation_id,
                    "error": None if success else "Failed to send message",
                }

            elif recipient_url:
                print(f"💬 Sending message to: {recipient_url}")

                # Extract profile ID from URL
                if "/in/" in recipient_url:
                    profile_id = recipient_url.split("/in/")[1].split("/")[0]
                    # Get the profile to extract URN ID
                    profile = client.get_profile(public_id=profile_id)
                    profile_urn = profile.get("profile_urn")
                    if not profile_urn:
                        return {
                            "success": False,
                            "error": "Could not determine recipient URN",
                        }

                    # Extract the URN ID from the profile URN
                    urn_id = profile_urn.split(":")[-1]

                    # Send the message
                    result = client.send_message(
                        message_body=message_body, recipients=[urn_id]
                    )
                    success = not result  # send_message returns False if successful

                    return {
                        "success": success,
                        "recipient_id": profile_id,
                        "error": None if success else "Failed to send message",
                    }
                else:
                    return {"success": False, "error": "Invalid recipient URL format"}
            else:
                return {
                    "success": False,
                    "error": "Either recipient_url or conversation_id must be provided",
                }
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return {"success": False, "error": f"Failed to send message: {str(e)}"}

    @mcp.tool()
    async def get_conversations() -> List[Dict[str, Any]]:
        """
        Get a list of the user's LinkedIn conversations.

        Returns:
            List[Dict[str, Any]]: List of conversations
        """
        try:
            client = LinkedInClientManager.get_client()

            print("📩 Retrieving conversations")

            # Get all conversations
            conversations = client.get_conversations()
            return conversations.get("elements", [])
        except Exception as e:
            logger.error(f"Error retrieving conversations: {e}")
            return [{"error": f"Failed to retrieve conversations: {str(e)}"}]

    @mcp.tool()
    async def get_conversation_messages(conversation_id: str) -> Dict[str, Any]:
        """
        Get messages from a specific LinkedIn conversation.

        Args:
            conversation_id (str): ID of the conversation to retrieve

        Returns:
            Dict[str, Any]: Conversation data with messages
        """
        try:
            client = LinkedInClientManager.get_client()

            print(f"💬 Retrieving messages for conversation: {conversation_id}")

            # Get conversation with messages
            conversation = client.get_conversation(conversation_id)
            return conversation
        except Exception as e:
            logger.error(f"Error retrieving conversation: {e}")
            return {"error": f"Failed to retrieve conversation: {str(e)}"}

    @mcp.tool()
    async def mark_conversation_as_seen(conversation_id: str) -> Dict[str, Any]:
        """
        Mark a LinkedIn conversation as seen/read.

        Args:
            conversation_id (str): ID of the conversation to mark as seen

        Returns:
            Dict[str, Any]: Result of the operation
        """
        try:
            client = LinkedInClientManager.get_client()

            print(f"✓ Marking conversation as seen: {conversation_id}")

            # Mark conversation as seen
            result = client.mark_conversation_as_seen(conversation_id)
            success = (
                not result
            )  # mark_conversation_as_seen returns False if successful

            return {
                "success": success,
                "conversation_id": conversation_id,
                "error": None if success else "Failed to mark conversation as seen",
            }
        except Exception as e:
            logger.error(f"Error marking conversation as seen: {e}")
            return {
                "success": False,
                "error": f"Failed to mark conversation as seen: {str(e)}",
            }
