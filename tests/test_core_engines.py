"""Tests for the engine adapter registry (core.engines).

Both BrowserManager's launch path and any cleanup/reset code resolve an
engine's on-disk profile directory through ENGINES[engine].profile_dir() --
these tests pin that single source of truth so it can't silently drift
between call sites again (see the profile-wipe incident these adapters
replaced the scattered `if engine == "camoufox"` branches for)."""

from linkedin_mcp_server.core.engines import ENGINES, CamoufoxAdapter, PatchrightAdapter


def test_engines_registry_has_both_adapters():
    assert set(ENGINES) == {"patchright", "camoufox"}
    assert isinstance(ENGINES["patchright"], PatchrightAdapter)
    assert isinstance(ENGINES["camoufox"], CamoufoxAdapter)


def test_patchright_profile_dir_is_user_data_dir_root(tmp_path):
    assert ENGINES["patchright"].profile_dir(tmp_path) == tmp_path


def test_camoufox_profile_dir_is_namespaced_subdirectory(tmp_path):
    resolved = ENGINES["camoufox"].profile_dir(tmp_path)
    assert resolved == tmp_path / "camoufox"
    # Never the shared root itself -- that's the exact incident this
    # namespacing prevents (a Camoufox reset must never touch it).
    assert resolved != tmp_path


def test_adapters_never_resolve_to_the_same_directory(tmp_path):
    assert ENGINES["patchright"].profile_dir(tmp_path) != ENGINES[
        "camoufox"
    ].profile_dir(tmp_path)


def test_patchright_needs_managed_install_unless_chrome_path_set():
    assert ENGINES["patchright"].needs_managed_install(None) is True
    assert ENGINES["patchright"].needs_managed_install("/usr/bin/chrome") is False


def test_camoufox_never_needs_managed_install():
    assert ENGINES["camoufox"].needs_managed_install(None) is False
    assert ENGINES["camoufox"].needs_managed_install("/usr/bin/chrome") is False


def test_supports_indexed_db_flags():
    assert ENGINES["patchright"].supports_indexed_db is True
    assert ENGINES["camoufox"].supports_indexed_db is False


def test_timeout_error_classes_are_distinct_between_engines():
    patchright_classes = set(ENGINES["patchright"].timeout_error_classes)
    camoufox_classes = set(ENGINES["camoufox"].timeout_error_classes)
    assert patchright_classes
    assert camoufox_classes
    assert patchright_classes.isdisjoint(camoufox_classes)
