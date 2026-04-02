"""
LinkedIn profile editing tools.

Provides tools to modify every section of the authenticated user's profile:
intro (name, headline, location), about/summary, experience, education,
skills, certifications, volunteer experience, projects, publications,
courses, languages, and honors.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_profile_edit_tools(mcp: FastMCP) -> None:
    """Register all profile editing tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Edit Profile Intro",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def edit_profile_intro(
        ctx: Context,
        first_name: str | None = None,
        last_name: str | None = None,
        headline: str | None = None,
        location: str | None = None,
        industry: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Edit the intro section of your LinkedIn profile.

        Only provided fields are changed; omitted fields stay as they are.

        Args:
            ctx: FastMCP context for progress reporting
            first_name: Your first name
            last_name: Your last name
            headline: Your profile headline (e.g., "Senior Software Engineer at Google")
            location: Your location (city or country/region)
            industry: Your industry

        Returns:
            Dict with url, status, message, and fields_updated list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="edit_profile_intro"
            )
            logger.info("Editing profile intro")

            await ctx.report_progress(
                progress=0, total=100, message="Opening intro edit form"
            )

            result = await extractor.edit_profile_intro(
                first_name=first_name,
                last_name=last_name,
                headline=headline,
                location=location,
                industry=industry,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "edit_profile_intro")
        except Exception as e:
            raise_tool_error(e, "edit_profile_intro")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Edit Profile About",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def edit_profile_about(
        about_text: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Edit the About/Summary section of your LinkedIn profile.

        Replaces the entire About section with the provided text.

        Args:
            about_text: The new About section text. Supports line breaks.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, and message.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="edit_profile_about"
            )
            logger.info("Editing profile about section")

            await ctx.report_progress(
                progress=0, total=100, message="Opening about edit form"
            )

            result = await extractor.edit_profile_about(about_text)

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "edit_profile_about")
        except Exception as e:
            raise_tool_error(e, "edit_profile_about")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Experience",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_experience(
        title: str,
        company: str,
        ctx: Context,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
        location: str | None = None,
        employment_type: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a new experience entry to your LinkedIn profile.

        Args:
            title: Job title (e.g., "Software Engineer")
            company: Company name (e.g., "Google")
            ctx: FastMCP context for progress reporting
            start_month: Start month (e.g., "January")
            start_year: Start year (e.g., "2023")
            end_month: End month, omit if current role
            end_year: End year, omit if current role
            description: Role description
            location: Work location
            employment_type: One of: Full-time, Part-time, Contract, Temporary,
                Volunteer, Internship, Freelance, Self-employed

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_experience"
            )
            logger.info("Adding experience: %s at %s", title, company)

            await ctx.report_progress(
                progress=0, total=100, message="Opening experience form"
            )

            result = await extractor.add_experience(
                title=title,
                company=company,
                start_month=start_month,
                start_year=start_year,
                end_month=end_month,
                end_year=end_year,
                description=description,
                location=location,
                employment_type=employment_type,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_experience")
        except Exception as e:
            raise_tool_error(e, "add_experience")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Education",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_education(
        school: str,
        ctx: Context,
        degree: str | None = None,
        field_of_study: str | None = None,
        start_year: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
        grade: str | None = None,
        activities: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a new education entry to your LinkedIn profile.

        Args:
            school: School name (e.g., "MIT")
            ctx: FastMCP context for progress reporting
            degree: Degree type (e.g., "Bachelor of Science")
            field_of_study: Field of study (e.g., "Computer Science")
            start_year: Start year (e.g., "2019")
            end_year: End year (e.g., "2023")
            description: Additional description
            grade: Grade/GPA
            activities: Activities and societies

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_education"
            )
            logger.info("Adding education: %s", school)

            await ctx.report_progress(
                progress=0, total=100, message="Opening education form"
            )

            result = await extractor.add_education(
                school=school,
                degree=degree,
                field_of_study=field_of_study,
                start_year=start_year,
                end_year=end_year,
                description=description,
                grade=grade,
                activities=activities,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_education")
        except Exception as e:
            raise_tool_error(e, "add_education")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Skill",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_skill(
        skill_name: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a skill to your LinkedIn profile.

        Args:
            skill_name: The skill to add (e.g., "Python", "Project Management")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, and message.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_skill"
            )
            logger.info("Adding skill: %s", skill_name)

            await ctx.report_progress(progress=0, total=100, message="Adding skill")

            result = await extractor.add_skill(skill_name)

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_skill")
        except Exception as e:
            raise_tool_error(e, "add_skill")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Certification",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_certification(
        name: str,
        issuing_organization: str,
        ctx: Context,
        issue_month: str | None = None,
        issue_year: str | None = None,
        expiration_month: str | None = None,
        expiration_year: str | None = None,
        credential_id: str | None = None,
        credential_url: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a certification to your LinkedIn profile.

        Args:
            name: Certification name (e.g., "AWS Solutions Architect")
            issuing_organization: Organization that issued it (e.g., "Amazon Web Services")
            ctx: FastMCP context for progress reporting
            issue_month: Month issued (e.g., "March")
            issue_year: Year issued (e.g., "2024")
            expiration_month: Expiration month, omit if no expiry
            expiration_year: Expiration year, omit if no expiry
            credential_id: Credential ID string
            credential_url: URL to verify the credential

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_certification"
            )
            logger.info("Adding certification: %s", name)

            await ctx.report_progress(
                progress=0, total=100, message="Opening certification form"
            )

            result = await extractor.add_certification(
                name=name,
                issuing_organization=issuing_organization,
                issue_month=issue_month,
                issue_year=issue_year,
                expiration_month=expiration_month,
                expiration_year=expiration_year,
                credential_id=credential_id,
                credential_url=credential_url,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_certification")
        except Exception as e:
            raise_tool_error(e, "add_certification")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Volunteer Experience",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_volunteer_experience(
        organization: str,
        role: str,
        ctx: Context,
        cause: str | None = None,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        description: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a volunteer experience to your LinkedIn profile.

        Args:
            organization: Organization name
            role: Your role/title
            ctx: FastMCP context for progress reporting
            cause: The cause (e.g., "Education", "Environment", "Health")
            start_month: Start month (e.g., "January")
            start_year: Start year (e.g., "2023")
            end_month: End month, omit if ongoing
            end_year: End year, omit if ongoing
            description: Description of your volunteer work

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_volunteer_experience"
            )
            logger.info("Adding volunteer: %s at %s", role, organization)

            await ctx.report_progress(
                progress=0, total=100, message="Opening volunteer form"
            )

            result = await extractor.add_volunteer_experience(
                organization=organization,
                role=role,
                cause=cause,
                start_month=start_month,
                start_year=start_year,
                end_month=end_month,
                end_year=end_year,
                description=description,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_volunteer_experience")
        except Exception as e:
            raise_tool_error(e, "add_volunteer_experience")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Project",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_project(
        name: str,
        ctx: Context,
        description: str | None = None,
        start_month: str | None = None,
        start_year: str | None = None,
        end_month: str | None = None,
        end_year: str | None = None,
        project_url: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a project to your LinkedIn profile.

        Args:
            name: Project name
            ctx: FastMCP context for progress reporting
            description: Project description
            start_month: Start month (e.g., "January")
            start_year: Start year (e.g., "2023")
            end_month: End month, omit if ongoing
            end_year: End year, omit if ongoing
            project_url: URL to the project

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_project"
            )
            logger.info("Adding project: %s", name)

            await ctx.report_progress(
                progress=0, total=100, message="Opening project form"
            )

            result = await extractor.add_project(
                name=name,
                description=description,
                start_month=start_month,
                start_year=start_year,
                end_month=end_month,
                end_year=end_year,
                project_url=project_url,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_project")
        except Exception as e:
            raise_tool_error(e, "add_project")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Publication",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_publication(
        title: str,
        ctx: Context,
        publisher: str | None = None,
        publication_date_month: str | None = None,
        publication_date_year: str | None = None,
        description: str | None = None,
        publication_url: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a publication to your LinkedIn profile.

        Args:
            title: Publication title
            ctx: FastMCP context for progress reporting
            publisher: Publisher name
            publication_date_month: Month published (e.g., "June")
            publication_date_year: Year published (e.g., "2024")
            description: Description of the publication
            publication_url: URL to the publication

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_publication"
            )
            logger.info("Adding publication: %s", title)

            await ctx.report_progress(
                progress=0, total=100, message="Opening publication form"
            )

            result = await extractor.add_publication(
                title=title,
                publisher=publisher,
                publication_date_month=publication_date_month,
                publication_date_year=publication_date_year,
                description=description,
                publication_url=publication_url,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_publication")
        except Exception as e:
            raise_tool_error(e, "add_publication")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Course",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_course(
        name: str,
        ctx: Context,
        number: str | None = None,
        associated_with: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a course to your LinkedIn profile.

        Args:
            name: Course name
            ctx: FastMCP context for progress reporting
            number: Course number/code
            associated_with: Associated education entry

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_course"
            )
            logger.info("Adding course: %s", name)

            await ctx.report_progress(progress=0, total=100, message="Adding course")

            result = await extractor.add_course(
                name=name,
                number=number,
                associated_with=associated_with,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_course")
        except Exception as e:
            raise_tool_error(e, "add_course")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Language",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_language(
        name: str,
        ctx: Context,
        proficiency: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add a language to your LinkedIn profile.

        Args:
            name: Language name (e.g., "Spanish", "Mandarin")
            ctx: FastMCP context for progress reporting
            proficiency: Proficiency level (e.g., "Native or bilingual",
                "Full professional", "Professional working",
                "Limited working", "Elementary")

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_language"
            )
            logger.info("Adding language: %s", name)

            await ctx.report_progress(progress=0, total=100, message="Adding language")

            result = await extractor.add_language(
                name=name,
                proficiency=proficiency,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_language")
        except Exception as e:
            raise_tool_error(e, "add_language")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Add Honor or Award",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"profile", "edit"},
        exclude_args=["extractor"],
    )
    async def add_honor(
        title: str,
        ctx: Context,
        issuer: str | None = None,
        issue_month: str | None = None,
        issue_year: str | None = None,
        description: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Add an honor or award to your LinkedIn profile.

        Args:
            title: Honor/award title
            ctx: FastMCP context for progress reporting
            issuer: Issuing organization
            issue_month: Month issued (e.g., "May")
            issue_year: Year issued (e.g., "2024")
            description: Description of the honor/award

        Returns:
            Dict with url, status, message, section, and fields_filled list.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="add_honor"
            )
            logger.info("Adding honor: %s", title)

            await ctx.report_progress(
                progress=0, total=100, message="Adding honor/award"
            )

            result = await extractor.add_honor(
                title=title,
                issuer=issuer,
                issue_month=issue_month,
                issue_year=issue_year,
                description=description,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "add_honor")
        except Exception as e:
            raise_tool_error(e, "add_honor")
