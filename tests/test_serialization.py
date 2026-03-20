"""Tests for output serialization utilities."""

from linkedin_mcp_server.serialization import strip_none


class TestStripNone:
    """Test strip_none with strict contract: only None removed, never falsy."""

    def test_removes_none_values(self):
        assert strip_none({"a": 1, "b": None}) == {"a": 1}

    def test_preserves_zero(self):
        assert strip_none({"a": 0}) == {"a": 0}

    def test_preserves_false(self):
        assert strip_none({"a": False}) == {"a": False}

    def test_preserves_empty_string(self):
        assert strip_none({"a": ""}) == {"a": ""}

    def test_preserves_empty_list(self):
        assert strip_none({"a": []}) == {"a": []}

    def test_preserves_empty_dict(self):
        assert strip_none({"a": {}}) == {"a": {}}

    def test_recursive_dict(self):
        data = {"a": {"b": None, "c": 1}, "d": None}
        assert strip_none(data) == {"a": {"c": 1}}

    def test_recursive_list_of_dicts(self):
        data = [{"a": None, "b": 1}, {"c": None}]
        assert strip_none(data) == [{"b": 1}, {}]

    def test_nested_list_in_dict(self):
        data = {"posts": [{"id": 1, "created_at": None}, {"id": 2, "text": "hi"}]}
        expected = {"posts": [{"id": 1}, {"id": 2, "text": "hi"}]}
        assert strip_none(data) == expected

    def test_empty_dict(self):
        assert strip_none({}) == {}

    def test_empty_list(self):
        assert strip_none([]) == []

    def test_all_none_values(self):
        assert strip_none({"a": None, "b": None}) == {}

    def test_non_dict_passthrough_string(self):
        assert strip_none("hello") == "hello"

    def test_non_dict_passthrough_int(self):
        assert strip_none(42) == 42

    def test_non_dict_passthrough_none(self):
        assert strip_none(None) is None

    def test_deeply_nested(self):
        data = {"l1": {"l2": {"l3": {"keep": "yes", "drop": None}}}}
        expected = {"l1": {"l2": {"l3": {"keep": "yes"}}}}
        assert strip_none(data) == expected

    def test_mixed_list_items(self):
        data = [1, None, "text", {"a": None, "b": 2}]
        expected = [1, None, "text", {"b": 2}]
        assert strip_none(data) == expected

    def test_realistic_tool_output(self):
        """Simulate a real posts tool output with None fields."""
        data = {
            "posts": [
                {
                    "post_url": "https://linkedin.com/feed/update/123",
                    "post_id": "123",
                    "text_preview": "Hello world",
                    "created_at": None,
                    "comment_permalink": None,
                },
                {
                    "post_url": "https://linkedin.com/feed/update/456",
                    "post_id": None,
                    "text_preview": "Another post",
                    "created_at": "2d ago",
                    "comment_permalink": None,
                },
            ]
        }
        result = strip_none(data)
        assert result == {
            "posts": [
                {
                    "post_url": "https://linkedin.com/feed/update/123",
                    "post_id": "123",
                    "text_preview": "Hello world",
                },
                {
                    "post_url": "https://linkedin.com/feed/update/456",
                    "text_preview": "Another post",
                    "created_at": "2d ago",
                },
            ]
        }
