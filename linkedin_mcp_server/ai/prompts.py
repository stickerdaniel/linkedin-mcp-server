"""
Prompt templates for AI-generated connection messages.

Provides templates for generating personalized LinkedIn connection
messages based on profile information.
"""

# System prompt for message generation
SYSTEM_PROMPT = """You are an expert at writing personalized LinkedIn connection messages.
Your messages should be:
- Professional but warm and personable
- Brief (under 300 characters to fit LinkedIn's limit)
- Specific to the person's background when possible
- Clear about why you want to connect
- Free of generic phrases like "I'd love to add you to my network"

Never use emojis or exclamation points excessively.
Write as a human professional would, not a salesperson."""


# Template for generating a connection message with context
MESSAGE_GENERATION_TEMPLATE = """Write a LinkedIn connection request message for the following person.

**Their Profile:**
- Name: {name}
- Current Title: {title}
- Company: {company}
- Location: {location}

**My Context:**
- My Role: {sender_role}
- My Company: {sender_company}
- Connection Reason: {reason}

**Requirements:**
- Maximum 300 characters
- Personalized to their background
- Professional but friendly tone
- Include why I want to connect

Write only the message text, no quotes or explanations."""


# Template when minimal profile info is available
MINIMAL_MESSAGE_TEMPLATE = """Write a brief LinkedIn connection request message.

**Their Name:** {name}
**Reason for connecting:** {reason}

**Requirements:**
- Maximum 300 characters
- Professional and friendly
- Direct about why I want to connect

Write only the message text, no quotes or explanations."""


# Template variations by connection reason
REASON_TEMPLATES = {
    "networking": "I'm expanding my professional network in {industry} and your background caught my attention.",
    "job_seeking": "I'm exploring opportunities in {field} and would value connecting with professionals like yourself.",
    "recruiting": "I'm always looking to connect with talented {role} professionals.",
    "learning": "Your experience in {field} is impressive and I'd love to learn from your insights.",
    "collaboration": "I see potential for collaboration between our work in {field}.",
    "mutual_connection": "We have mutual connections and I thought we should connect directly.",
    "same_company": "I noticed we both work at {company} and wanted to connect.",
    "same_school": "Fellow {school} alum here! Would love to connect.",
    "event": "Great meeting you at {event}! Let's stay connected.",
    "content": "I've been following your posts about {topic} and find them valuable.",
}


def get_reason_template(reason: str, **kwargs: str) -> str:
    """
    Get a formatted reason template.

    Args:
        reason: The reason type
        **kwargs: Template variables

    Returns:
        Formatted reason string
    """
    template = REASON_TEMPLATES.get(reason, REASON_TEMPLATES["networking"])
    try:
        return template.format(**kwargs)
    except KeyError:
        return template
