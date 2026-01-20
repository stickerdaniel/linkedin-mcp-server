"""
Diagnostic script to test path expansion on different platforms.

This helps diagnose Windows-specific path issues with session file creation.
"""

import sys
from pathlib import Path


def test_path_expansion():
    """Test various path expansion scenarios."""
    print("=" * 60)
    print("Path Expansion Diagnostic Tool")
    print("=" * 60)
    print()

    # System info
    print(f"Platform: {sys.platform}")
    print(f"Python version: {sys.version}")
    print()

    # Test 1: Path.home()
    print("Test 1: Path.home()")
    print("-" * 60)
    try:
        home = Path.home()
        print(f"✓ Path.home() = {home}")
        print(f"  Type: {type(home)}")
        print(f"  Exists: {home.exists()}")
        print(f"  Absolute: {home.resolve()}")
    except Exception as e:
        print(f"✗ Error: {e}")
    print()

    # Test 2: Tilde expansion
    print("Test 2: Tilde expansion with expanduser()")
    print("-" * 60)
    test_paths = [
        "~/.linkedin-mcp/session.json",
        "~/test/path",
        "~",
    ]

    for test_path in test_paths:
        try:
            expanded = Path(test_path).expanduser()
            print(f"  '{test_path}'")
            print(f"    → {expanded}")
            print(f"    → Absolute: {expanded.resolve()}")
        except Exception as e:
            print(f"  '{test_path}' → Error: {e}")
    print()

    # Test 3: DEFAULT_SESSION_PATH simulation
    print(
        "Test 3: DEFAULT_SESSION_PATH (Path.home() / '.linkedin-mcp' / 'session.json')"
    )
    print("-" * 60)
    try:
        default_path = Path.home() / ".linkedin-mcp" / "session.json"
        print(f"  Path: {default_path}")
        print(f"  Type: {type(default_path)}")
        print(f"  Absolute: {default_path.resolve()}")
        print(f"  Parent: {default_path.parent}")
        print(f"  Parent exists: {default_path.parent.exists()}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # Test 4: Directory creation
    print("Test 4: Parent directory creation test")
    print("-" * 60)
    test_dir = Path.home() / ".linkedin-mcp-test"
    try:
        print(f"  Test directory: {test_dir}")
        print("  Creating parent directories...")
        test_dir.mkdir(parents=True, exist_ok=True)
        print("  ✓ Directory created successfully")
        print(f"  Exists: {test_dir.exists()}")
        print(f"  Is directory: {test_dir.is_dir()}")

        # Cleanup
        test_dir.rmdir()
        print("  ✓ Cleanup successful")
    except Exception as e:
        print(f"  ✗ Error: {e}")
    print()

    # Test 5: String conversion
    print("Test 5: Path to string conversion")
    print("-" * 60)
    session_path = Path.home() / ".linkedin-mcp" / "session.json"
    path_str = str(session_path)
    print(f"  Path object: {session_path}")
    print(f"  As string: {path_str}")
    print(f"  String type: {type(path_str)}")

    # Verify round-trip
    round_trip = Path(path_str)
    print(f"  Round-trip: {round_trip}")
    print(f"  Equal: {session_path == round_trip}")
    print()

    # Summary
    print("=" * 60)
    print("Diagnostic Summary")
    print("=" * 60)
    print("If all tests passed, path handling should work correctly.")
    print("If any test failed, please report the error with your platform info.")
    print()


if __name__ == "__main__":
    test_path_expansion()
