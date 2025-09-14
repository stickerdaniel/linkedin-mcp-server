#!/usr/bin/env python
"""
Tests for enhanced contact info extraction functionality.

These tests verify the ContactExtended class and contact info extraction
capabilities without requiring actual LinkedIn authentication.
"""

import pytest
import unittest.mock as mock
from unittest.mock import MagicMock, Mock

from linkedin_mcp_server.scrapers.person_extended import PersonExtended


class TestContactExtraction:
    """Test suite for contact information extraction."""

    def test_contact_info_initialization(self):
        """Test that PersonExtended initializes contact_info properly."""
        # Mock the parent class to avoid selenium initialization
        with mock.patch('linkedin_scraper.Person.__init__'):
            person = PersonExtended.__new__(PersonExtended)
            person.__init__(linkedin_url="test", driver=None, get=False, scrape=False)

            # Verify contact_info structure
            assert hasattr(person, 'contact_info')
            assert isinstance(person.contact_info, dict)

            expected_fields = ['email', 'phone', 'birthday', 'connected_date', 'websites', 'linkedin_url']
            for field in expected_fields:
                assert field in person.contact_info

    def test_to_dict_includes_contact_info(self):
        """Test that to_dict method includes contact_info in output."""
        with mock.patch('linkedin_scraper.Person.__init__'):
            person = PersonExtended.__new__(PersonExtended)
            person.__init__(linkedin_url="test", driver=None, get=False, scrape=False)

            # Set some test data
            person.name = "Test User"
            person.experiences = []
            person.educations = []
            person.interests = []
            person.accomplishments = []
            person.contacts = []
            person.contact_info = {
                "email": "test@example.com",
                "phone": "+1-555-0123",
                "birthday": "Jan 1",
                "connected_date": "2023",
                "websites": [],
                "linkedin_url": "https://linkedin.com/in/test"
            }

            result = person.to_dict()

            # Verify structure
            assert 'contact_info' in result
            assert result['contact_info']['email'] == "test@example.com"
            assert result['contact_info']['phone'] == "+1-555-0123"

    def test_extract_contact_info_method_exists(self):
        """Test that extract_contact_info method is available."""
        assert hasattr(PersonExtended, 'extract_contact_info')
        assert hasattr(PersonExtended, '_extract_modal_data')
        assert hasattr(PersonExtended, '_close_modal')

    def test_contact_info_field_types(self):
        """Test that contact info fields have correct types."""
        with mock.patch('linkedin_scraper.Person.__init__'):
            person = PersonExtended.__new__(PersonExtended)
            person.__init__(linkedin_url="test", driver=None, get=False, scrape=False)

            contact = person.contact_info

            # Test initial types
            assert contact['email'] is None or isinstance(contact['email'], str)
            assert contact['phone'] is None or isinstance(contact['phone'], str)
            assert contact['birthday'] is None or isinstance(contact['birthday'], str)
            assert contact['connected_date'] is None or isinstance(contact['connected_date'], str)
            assert contact['linkedin_url'] is None or isinstance(contact['linkedin_url'], str)
            assert isinstance(contact['websites'], list)

    def test_modal_selectors_are_comprehensive(self):
        """Test that we have comprehensive selectors for modal detection."""
        # This test ensures we don't lose selectors in future changes
        import inspect

        # Get the source code of _extract_modal_data
        source = inspect.getsource(PersonExtended._extract_modal_data)

        # Check that multiple selector strategies are present
        assert "[role='dialog']" in source or ".artdeco-modal" in source
        assert "modal" in source.lower()

    def test_email_extraction_strategies(self):
        """Test that multiple email extraction strategies are implemented."""
        import inspect

        source = inspect.getsource(PersonExtended._extract_modal_data)

        # Check for multiple email extraction approaches
        assert "mailto:" in source
        assert "email" in source.lower()

    def test_phone_extraction_strategies(self):
        """Test that multiple phone extraction strategies are implemented."""
        import inspect

        source = inspect.getsource(PersonExtended._extract_modal_data)

        # Check for phone extraction approaches
        assert "phone" in source.lower()


class TestPersonExtendedIntegration:
    """Integration tests for PersonExtended (no actual LinkedIn calls)."""

    def test_graceful_failure_without_driver(self):
        """Test that PersonExtended handles missing driver gracefully."""
        with mock.patch('linkedin_scraper.Person.__init__'):
            person = PersonExtended.__new__(PersonExtended)
            person.__init__(linkedin_url="test", driver=None, get=False, scrape=False)

            # Should not raise exception
            result = person.to_dict()
            assert 'contact_info' in result

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
