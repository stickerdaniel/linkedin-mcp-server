# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Enhanced Contact Information Extraction** in `get_person_profile` tool
  - Automatically extracts email addresses from LinkedIn Contact Info modal
  - Extracts phone numbers, websites, birthday, and connection dates
  - Returns structured `contact_info` object with all available contact details
  - Optional `extract_contact_info` parameter to control this behavior (default: true)
  - Robust element detection with multiple selector strategies for reliability
  - JavaScript click fallback for improved modal interaction

### Fixed
- **ChromeDriver Version Compatibility Issues**
  - Replaced manual ChromeDriver management with automatic version detection
  - Added `webdriver-manager>=4.0.0` dependency for automatic driver updates
  - ChromeDriver now automatically matches installed Chrome version (138/140+ compatibility)
  - Eliminated system PATH fallback that caused version mismatches
  - Added comprehensive error handling and version verification logging

### Technical Improvements
- **Enhanced Person Scraping Architecture**
  - New `PersonExtended` class extending `linkedin_scraper.Person`
  - Modular design with separate methods for contact info extraction
  - Improved error handling with graceful fallbacks
  - Added comprehensive logging for debugging and monitoring
- **Better Modal Interaction**
  - Multiple XPath selectors for Contact Info link detection
  - Robust modal waiting and data extraction
  - Proper modal cleanup after data extraction

### Developer Experience
- Enhanced debugging capabilities with detailed logging
- Improved error messages and troubleshooting information
- Better separation of concerns in the codebase architecture

## Data Structure Changes

### get_person_profile Output Enhancement

The `get_person_profile` tool now returns an enhanced data structure:

```json
{
  "name": "John Doe",
  "job_title": "Software Engineer",
  "company": "Tech Corp",
  "contact_info": {
    "email": "john@example.com",
    "phone": "+1-555-0123",
    "birthday": "January 15",
    "connected_date": "March 2023",
    "linkedin_url": "https://linkedin.com/in/johndoe",
    "websites": [
      {"url": "https://portfolio.com", "type": "Portfolio"},
      {"url": "https://company.com", "type": "Company Website"}
    ]
  },
  "experiences": [...],
  "educations": [...],
  "interests": [...],
  "accomplishments": [...],
  "contacts": [...]
}
```

### Backward Compatibility

- All existing functionality remains unchanged
- Contact info extraction is enabled by default but can be disabled
- API signatures are backward compatible
- No breaking changes to existing tool usage

## Migration Notes

### For Existing Users
1. Update your LinkedIn MCP Server to the latest version
2. Restart Claude Desktop to pick up the new functionality
3. Contact information will now be automatically included in profile results
4. No configuration changes required - ChromeDriver compatibility is handled automatically

### For Developers
1. The new `PersonExtended` class can be used directly if extending functionality
2. Contact info extraction methods are available for custom implementations
3. All dependencies are managed automatically via `webdriver-manager`

## Known Issues

### Contact Information Availability
- Contact info extraction depends on LinkedIn's privacy settings
- Users may restrict contact information to connections only
- Some profiles may not have publicly visible contact details
- The tool gracefully handles these cases with null values

### Browser Compatibility
- Requires Chrome browser to be installed
- ChromeDriver is now managed automatically (no manual installation needed)
- Headless mode is used by default for privacy and performance
