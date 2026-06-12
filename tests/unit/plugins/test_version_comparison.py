# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for PluginLoader._version_is_newer()."""

from qwenpaw.plugins.loader import PluginLoader


class TestVersionIsNewer:
    """Cover simple, PEP 440, and edge-case version strings."""

    # ---- Basic semantic versioning ----

    def test_newer_patch(self):
        assert PluginLoader._version_is_newer("1.0.1", "1.0.0")

    def test_newer_minor(self):
        assert PluginLoader._version_is_newer("1.1.0", "1.0.9")

    def test_newer_major(self):
        assert PluginLoader._version_is_newer("2.0.0", "1.9.9")

    def test_equal_versions(self):
        assert not PluginLoader._version_is_newer("1.0.0", "1.0.0")

    def test_older_version(self):
        assert not PluginLoader._version_is_newer("1.0.0", "1.0.1")

    # ---- PEP 440 pre-release / post-release ----

    def test_release_beats_pre_release(self):
        assert PluginLoader._version_is_newer("1.0.0", "1.0.0rc1")

    def test_pre_release_ordering(self):
        assert PluginLoader._version_is_newer("1.0.0rc2", "1.0.0rc1")

    def test_post_release(self):
        assert PluginLoader._version_is_newer("1.0.0.post1", "1.0.0")

    def test_dev_release(self):
        assert PluginLoader._version_is_newer("1.0.0", "1.0.0.dev1")

    def test_alpha_beta_ordering(self):
        assert PluginLoader._version_is_newer("1.0.0b1", "1.0.0a2")

    # ---- Edge cases ----

    def test_different_length_tuples(self):
        assert PluginLoader._version_is_newer("1.0.0.1", "1.0.0")

    def test_empty_strings(self):
        assert not PluginLoader._version_is_newer("", "")

    def test_same_string(self):
        assert not PluginLoader._version_is_newer("0.1.0", "0.1.0")
