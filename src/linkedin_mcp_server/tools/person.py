# src/linkedin_mcp_server/tools/person.py
"""
Person profile tools for LinkedIn MCP server.

This module provides tools for accessing LinkedIn person profiles.
"""

from typing import Dict, Any, List
import logging
from mcp.server.fastmcp import FastMCP

from linkedin_mcp_server.client import LinkedInClientManager

logger = logging.getLogger(__name__)


def register_person_tools(mcp: FastMCP) -> None:
    """
    Register all person-related tools with the MCP server.

    Args:
        mcp (FastMCP): The MCP server instance
    """

    @mcp.tool()
    async def get_person_profile(linkedin_url: str) -> Dict[str, Any]:
        """
        Scrape a person's LinkedIn profile.

        Args:
            linkedin_url (str): The LinkedIn URL of the person's profile

        Returns:
            Dict[str, Any]: Structured data from the person's profile
        """
        try:
            client = LinkedInClientManager.get_client()

            # Extract public_id from URL
            if "/in/" in linkedin_url:
                public_id = linkedin_url.split("/in/")[1].split("/")[0]
            else:
                public_id = linkedin_url  # Assume it's already a public ID

            print(f"🔍 Retrieving profile: {public_id}")

            # Get comprehensive profile data
            profile = client.get_profile(public_id=public_id)

            # Enrich with contact information
            try:
                contact_info = client.get_profile_contact_info(public_id=public_id)
                if contact_info:
                    profile["contact_info"] = contact_info
            except Exception as contact_e:
                logger.warning(f"Could not retrieve contact info: {contact_e}")

            # Try to get skills
            try:
                skills = client.get_profile_skills(public_id=public_id)
                if skills:
                    profile["detailed_skills"] = skills
            except Exception as skills_e:
                logger.warning(f"Could not retrieve skills: {skills_e}")

            return profile
        except Exception as e:
            logger.error(f"Error retrieving profile: {e}")
            return {"error": f"Failed to retrieve profile: {str(e)}"}

    @mcp.tool()
    async def get_profile_connections(profile_url: str) -> List[Dict[str, Any]]:
        """
        Get connections for a LinkedIn profile.

        Args:
            profile_url (str): URL or ID of the LinkedIn profile

        Returns:
            List[Dict[str, Any]]: List of connections
        """
        try:
            client = LinkedInClientManager.get_client()

            # First, get the profile to extract the URN ID
            if "/in/" in profile_url:
                public_id = profile_url.split("/in/")[1].split("/")[0]
                profile = client.get_profile(public_id=public_id)
                urn_id = profile.get("urn_id")
            else:
                # Assume it's already a URN ID
                urn_id = profile_url

            if not urn_id:
                return [{"error": "Could not determine profile URN ID"}]

            print(f"👥 Retrieving connections for: {profile_url}")

            # Get connections
            connections = client.get_profile_connections(urn_id=urn_id)
            return connections
        except Exception as e:
            logger.error(f"Error retrieving connections: {e}")
            return [{"error": f"Failed to retrieve connections: {str(e)}"}]

    @mcp.tool()
    async def get_profile_experiences(profile_url: str) -> List[Dict[str, Any]]:
        """
        Get detailed work experiences for a LinkedIn profile.

        Args:
            profile_url (str): URL or ID of the LinkedIn profile

        Returns:
            List[Dict[str, Any]]: List of experiences with details
        """
        try:
            client = LinkedInClientManager.get_client()

            # First, get the profile to extract the URN ID
            if "/in/" in profile_url:
                public_id = profile_url.split("/in/")[1].split("/")[0]
                profile = client.get_profile(public_id=public_id)
                urn_id = profile.get("urn_id")
            else:
                # Assume it's already a URN ID
                urn_id = profile_url

            if not urn_id:
                return [{"error": "Could not determine profile URN ID"}]

            print(f"💼 Retrieving experiences for: {profile_url}")

            # Get detailed experiences
            experiences = client.get_profile_experiences(urn_id=urn_id)
            return experiences
        except Exception as e:
            logger.error(f"Error retrieving experiences: {e}")
            return [{"error": f"Failed to retrieve experiences: {str(e)}"}]

    @mcp.tool()
    async def get_profile_posts(
        profile_url: str, post_count: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get recent posts from a LinkedIn profile.

        Args:
            profile_url (str): URL or ID of the LinkedIn profile
            post_count (int): Number of posts to retrieve (max 100)

        Returns:
            List[Dict[str, Any]]: List of posts
        """
        try:
            client = LinkedInClientManager.get_client()

            # First, get the profile to extract the URN ID
            if "/in/" in profile_url:
                public_id = profile_url.split("/in/")[1].split("/")[0]
                profile = client.get_profile(public_id=public_id)
                urn_id = profile.get("urn_id")
            else:
                # Assume it's already a URN ID
                urn_id = profile_url

            if not urn_id:
                return [{"error": "Could not determine profile URN ID"}]

            print(f"📱 Retrieving posts for: {profile_url}")

            # Get posts (limited to requested count)
            posts = client.get_profile_posts(
                urn_id=urn_id, post_count=min(post_count, 100)
            )
            return posts
        except Exception as e:
            logger.error(f"Error retrieving posts: {e}")
            return [{"error": f"Failed to retrieve posts: {str(e)}"}]
