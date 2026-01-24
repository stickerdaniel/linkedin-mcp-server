"""
AI-powered connection message generator.

Generates personalized LinkedIn connection messages using templates
and profile information. This module provides local template-based
generation that can be extended with LLM integration.
"""

import random
from typing import Any

from .prompts import get_reason_template


class ConnectionMessageGenerator:
    """
    Generates personalized LinkedIn connection messages.

    Uses templates and profile information to create relevant,
    personalized connection messages under 300 characters.
    """

    # Pre-written message templates for quick generation
    QUICK_TEMPLATES = [
        "Hi {name}, I came across your profile and was impressed by your work at {company}. Would love to connect!",
        "Hello {name}, your experience in {field} caught my attention. I'd appreciate the opportunity to connect.",
        "Hi {name}, I'm building my network in {industry} and your background is exactly what I'm looking to learn from.",
        "Hi {name}, I noticed we share an interest in {field}. Would be great to connect and exchange ideas.",
        "Hello {name}, your work at {company} is impressive. I'd value having you in my network.",
    ]

    GENERIC_TEMPLATES = [
        "Hi {name}, I'd love to connect and learn more about your work.",
        "Hello {name}, your profile stood out to me. Would be great to connect!",
        "Hi {name}, I'm expanding my professional network and would appreciate connecting with you.",
    ]

    def __init__(self):
        """Initialize the message generator."""
        pass

    def generate(
        self,
        name: str,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        reason: str | None = None,
        sender_role: str | None = None,
        sender_company: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Generate a personalized connection message.

        Args:
            name: The person's name
            title: Their job title
            company: Their company name
            location: Their location
            reason: Reason for connecting (e.g., networking, job_seeking)
            sender_role: Your job title
            sender_company: Your company name
            **kwargs: Additional context

        Returns:
            Dictionary containing:
            - message: The generated message (max 300 chars)
            - template_used: Which template was used
            - personalization_score: How personalized (0-100)
        """
        # Extract first name
        first_name = name.split()[0] if name else "there"

        # Determine field/industry from title
        field = self._extract_field(title)
        industry = self._extract_industry(company, title)

        # Build context for template
        context = {
            "name": first_name,
            "full_name": name,
            "title": title or "your role",
            "company": company or "your company",
            "location": location or "",
            "field": field,
            "industry": industry,
            "reason": reason or "networking",
            "sender_role": sender_role or "",
            "sender_company": sender_company or "",
        }

        # Choose generation strategy based on available info
        if company and title:
            message, template = self._generate_personalized(context)
            score = 80
        elif company or title:
            message, template = self._generate_semi_personalized(context)
            score = 50
        else:
            message, template = self._generate_generic(context)
            score = 20

        # Ensure message is under 300 characters
        message = self._truncate_message(message)

        return {
            "message": message,
            "template_used": template,
            "personalization_score": score,
            "context": {
                "name": name,
                "title": title,
                "company": company,
            },
        }

    def generate_from_reason(
        self,
        name: str,
        reason: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Generate a message based on a specific connection reason.

        Args:
            name: The person's name
            reason: Connection reason type (see REASON_TEMPLATES)
            **kwargs: Variables for the reason template

        Returns:
            Dictionary with the generated message
        """
        first_name = name.split()[0] if name else "there"

        # Get reason-specific opener
        reason_text = get_reason_template(reason, **kwargs)

        # Combine with greeting
        message = f"Hi {first_name}, {reason_text} Would love to connect!"

        message = self._truncate_message(message)

        return {
            "message": message,
            "template_used": f"reason_{reason}",
            "personalization_score": 60,
            "reason": reason,
        }

    def _generate_personalized(self, context: dict[str, Any]) -> tuple[str, str]:
        """Generate a highly personalized message."""
        template = random.choice(self.QUICK_TEMPLATES)
        message = template.format(**context)
        return message, "personalized"

    def _generate_semi_personalized(self, context: dict[str, Any]) -> tuple[str, str]:
        """Generate a semi-personalized message."""
        if context.get("company"):
            message = f"Hi {context['name']}, I noticed your work at {context['company']} and would love to connect!"
        elif context.get("title"):
            message = f"Hi {context['name']}, your experience as {context['title']} caught my attention. Let's connect!"
        else:
            message = (
                f"Hi {context['name']}, I'd love to add you to my professional network."
            )

        return message, "semi_personalized"

    def _generate_generic(self, context: dict[str, Any]) -> tuple[str, str]:
        """Generate a generic but friendly message."""
        template = random.choice(self.GENERIC_TEMPLATES)
        message = template.format(**context)
        return message, "generic"

    def _extract_field(self, title: str | None) -> str:
        """Extract a general field from job title."""
        if not title:
            return "your field"

        title_lower = title.lower()

        field_keywords = {
            "software": "software development",
            "engineer": "engineering",
            "product": "product management",
            "design": "design",
            "marketing": "marketing",
            "sales": "sales",
            "data": "data science",
            "finance": "finance",
            "hr": "human resources",
            "operations": "operations",
            "consulting": "consulting",
            "research": "research",
        }

        for keyword, field in field_keywords.items():
            if keyword in title_lower:
                return field

        return "your field"

    def _extract_industry(self, company: str | None, title: str | None) -> str:
        """Extract industry from company or title."""
        if not company and not title:
            return "your industry"

        combined = f"{company or ''} {title or ''}".lower()

        industry_keywords = {
            "tech": "technology",
            "software": "technology",
            "bank": "finance",
            "financial": "finance",
            "health": "healthcare",
            "medical": "healthcare",
            "education": "education",
            "university": "education",
            "retail": "retail",
            "manufacturing": "manufacturing",
            "consulting": "consulting",
            "media": "media",
            "entertainment": "entertainment",
        }

        for keyword, industry in industry_keywords.items():
            if keyword in combined:
                return industry

        return "your industry"

    def _truncate_message(self, message: str) -> str:
        """Ensure message is under 300 characters."""
        if len(message) <= 300:
            return message

        # Try to truncate at a sentence boundary
        truncated = message[:297]
        last_period = truncated.rfind(".")
        last_exclaim = truncated.rfind("!")

        boundary = max(last_period, last_exclaim)
        if boundary > 200:
            return message[: boundary + 1]

        # Otherwise just truncate with ellipsis
        return message[:297] + "..."


# Singleton instance
_generator: ConnectionMessageGenerator | None = None


def get_message_generator() -> ConnectionMessageGenerator:
    """Get the global message generator instance."""
    global _generator
    if _generator is None:
        _generator = ConnectionMessageGenerator()
    generator = _generator
    assert generator is not None
    return generator
