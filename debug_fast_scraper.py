#!/usr/bin/env python3
"""
Debug script to test fast-linkedin-scraper directly without our wrapper.
"""

import threading


def test_fast_scraper_direct():
    """Test fast-linkedin-scraper directly."""
    print("Testing fast-linkedin-scraper directly...")

    try:
        from fast_linkedin_scraper import LinkedInSession

        print("✅ fast-linkedin-scraper imported successfully")

        # Test getting version info
        try:
            import fast_linkedin_scraper

            print(
                f"📦 fast-linkedin-scraper version: {getattr(fast_linkedin_scraper, '__version__', 'unknown')}"
            )
        except Exception:
            print("📦 Version info not available")
    except ImportError as e:
        print(f"❌ Cannot import fast-linkedin-scraper: {e}")
        return False

    # Test with a dummy cookie to see if the library initializes properly
    dummy_cookie = "li_at=dummy_cookie_for_testing"

    try:
        print("🔍 Testing LinkedInSession creation...")
        with LinkedInSession.from_cookie(dummy_cookie) as session:
            print(f"✅ Session created successfully: {type(session)}")
            print(
                "This would normally fail with invalid cookie, but creation succeeded"
            )

    except Exception as e:
        error_msg = str(e).lower()
        print(f"❌ Session creation failed: {e}")

        if (
            "'playwrightcontextmanager' object has no attribute '_connection'"
            in error_msg
        ):
            print("🐛 This is the _connection attribute error!")
            return False
        elif "invalid" in error_msg and "cookie" in error_msg:
            print(
                "✅ Failed due to invalid cookie (expected), but no _connection error!"
            )
            return True
        else:
            print(f"❓ Unknown error: {error_msg}")
            return False

    return True


def test_in_thread():
    """Test fast-linkedin-scraper in a separate thread."""
    print("\n" + "=" * 50)
    print("Testing fast-linkedin-scraper in a separate thread...")

    result_container = {}

    def thread_target():
        try:
            result_container["result"] = test_fast_scraper_direct()
        except Exception as e:
            result_container["error"] = e

    thread = threading.Thread(target=thread_target)
    thread.start()
    thread.join()

    if "error" in result_container:
        print(f"❌ Thread test failed: {result_container['error']}")
        return False

    return result_container.get("result", False)


if __name__ == "__main__":
    print("🚀 Fast-LinkedIn-Scraper Debug Test\n")

    # Test 1: Direct execution
    print("=" * 50)
    direct_result = test_fast_scraper_direct()

    # Test 2: In thread (simulates our async fix)
    thread_result = test_in_thread()

    print("\n" + "=" * 50)
    print("📊 RESULTS:")
    print(f"Direct test: {'✅ PASSED' if direct_result else '❌ FAILED'}")
    print(f"Thread test: {'✅ PASSED' if thread_result else '❌ FAILED'}")

    if direct_result and thread_result:
        print("\n🎉 fast-linkedin-scraper works correctly!")
    else:
        print("\n💥 fast-linkedin-scraper has compatibility issues!")
        print("📋 Recommendations:")
        print("   1. Check fast-linkedin-scraper installation")
        print("   2. Ensure playwright is installed: playwright install")
        print(
            "   3. Try upgrading: pip install --upgrade fast-linkedin-scraper playwright"
        )
