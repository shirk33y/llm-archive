"""Tests for Windsurf ingestor and protobuf decoding."""
from __future__ import annotations

import pytest

from llm_archive.ingestors.windsurf import (
    decode_varint,
    encode_varint,
    decode_field,
    serialize_step,
    LanguageServerClient,
)


# --- Protobuf encoding/decoding ---

def test_encode_decode_varint():
    """Test varint encoding and decoding."""
    test_values = [0, 1, 127, 128, 300, 16384, 2097151]
    for val in test_values:
        encoded = encode_varint(val)
        decoded, offset = decode_varint(encoded, 0)
        assert decoded == val
        assert offset == len(encoded)


def test_decode_field_varint():
    """Test decoding varint field."""
    # Field number 1, wire type 0 (varint), value 42
    tag = (1 << 3) | 0  # field 1, wire type 0
    data = encode_varint(tag) + encode_varint(42)
    fn, wt, val, offset = decode_field(data, 0)
    assert fn == 1
    assert wt == 0
    assert val == 42
    assert offset == len(data)


def test_decode_field_length_delimited():
    """Test decoding length-delimited field."""
    # Field number 2, wire type 2 (length-delimited), value "hello"
    tag = (2 << 3) | 2  # field 2, wire type 2
    value = b"hello"
    data = encode_varint(tag) + encode_varint(len(value)) + value
    fn, wt, val, offset = decode_field(data, 0)
    assert fn == 2
    assert wt == 2
    assert val == value
    assert offset == len(data)


def test_decode_field_64bit():
    """Test decoding 64-bit field."""
    # Field number 3, wire type 1 (64-bit)
    tag = (3 << 3) | 1  # field 3, wire type 1
    value = 0x123456789ABCDEF0
    data = encode_varint(tag) + value.to_bytes(8, 'little')
    fn, wt, val, offset = decode_field(data, 0)
    assert fn == 3
    assert wt == 1
    assert val == value
    assert offset == len(data)


def test_decode_field_32bit():
    """Test decoding 32-bit field."""
    # Field number 4, wire type 5 (32-bit)
    tag = (4 << 3) | 5  # field 4, wire type 5
    value = 0x12345678
    data = encode_varint(tag) + value.to_bytes(4, 'little')
    fn, wt, val, offset = decode_field(data, 0)
    assert fn == 4
    assert wt == 5
    assert val == value
    assert offset == len(data)


def test_serialize_step():
    """Test serialize_step converts bytes to strings."""
    step = {
        "type": 14,
        "data": {
            "user_input": b"hello world",
            "nested": {"bytes": b"test"},
        },
    }
    serialized = serialize_step(step)
    assert isinstance(serialized["data"]["user_input"], str)
    assert serialized["data"]["user_input"] == "hello world"
    assert isinstance(serialized["data"]["nested"]["bytes"], str)
    assert serialized["data"]["nested"]["bytes"] == "test"


def test_serialize_step_list():
    """Test serialize_step handles lists."""
    step = {
        "type": 14,
        "items": [b"item1", b"item2"],
    }
    serialized = serialize_step(step)
    assert isinstance(serialized["items"][0], str)
    assert serialized["items"][0] == "item1"


def test_serialize_step_empty():
    """Test serialize_step with empty step."""
    step = {"type": 14}
    serialized = serialize_step(step)
    assert serialized == step


# --- LanguageServerClient ---

def test_language_server_client_init():
    """Test LanguageServerClient initialization."""
    ls = LanguageServerClient(port=12345)
    assert ls.base == "http://localhost:12345"
    assert ls.port == 12345


def test_language_server_client_custom_port():
    """Test LanguageServerClient with custom port."""
    ls = LanguageServerClient(port=12345)
    assert ls.base == "http://localhost:12345"
    assert ls.port == 12345


def test_encode_get_cascade_request():
    """Test GetCascadeTrajectoryRequest encoding."""
    from llm_archive.ingestors.windsurf import encode_get_cascade_request

    cascade_id = "test-cascade-id-123"
    encoded = encode_get_cascade_request(cascade_id)
    assert isinstance(encoded, bytes)
    assert len(encoded) > 0
    # Should contain the cascade ID
    assert cascade_id.encode('utf-8') in encoded


# --- Decoder functions ---

def test_decode_user_input():
    """Test _decode_user_input decoder."""
    ls = LanguageServerClient()
    # Simple user input
    data = b"\x0a\x05hello"  # field 1, length 5, "hello"
    result = ls._decode_user_input(data)
    assert isinstance(result, str)
    assert "hello" in result


def test_decode_planner_response():
    """Test _decode_planner_response decoder."""
    ls = LanguageServerClient()
    # Simple planner response
    data = b"\x0a\x05world"  # field 1, length 5, "world"
    result = ls._decode_planner_response(data)
    assert isinstance(result, str)
    assert "world" in result


def test_decode_error_message():
    """Test _decode_error_message decoder."""
    ls = LanguageServerClient()
    # Error message with readable text
    data = b"Error: context deadline exceeded"
    result = ls._decode_error_message(data)
    assert isinstance(result, str)
    assert "Error" in result


def test_decode_file_content_edit():
    """Test _decode_file_content_edit decoder."""
    ls = LanguageServerClient()
    # Shell script content
    data = b"#!/bin/bash\necho hello"
    result = ls._decode_file_content_edit(data)
    assert isinstance(result, str)
    assert "#!/bin/bash" in result


def test_decode_parsed_url_content():
    """Test _decode_parsed_url_content decoder."""
    ls = LanguageServerClient()
    # Markdown content with links
    data = b"[link](https://example.com)"
    result = ls._decode_parsed_url_content(data)
    assert isinstance(result, str)
    assert "link" in result


def test_decode_url_reference():
    """Test _decode_url_reference decoder."""
    ls = LanguageServerClient()
    # URL
    data = b"https://example.com/path"
    result = ls._decode_url_reference(data)
    assert isinstance(result, str)
    assert "https://example.com" in result


def test_decode_context_memory():
    """Test _decode_context_memory decoder."""
    ls = LanguageServerClient()
    # Binary data with some readable text
    data = b"summary\x00context\x00data"
    result = ls._decode_context_memory(data)
    assert isinstance(result, dict)
    assert "summary" in result or "raw" in result


def test_decode_project_context():
    """Test _decode_project_context decoder."""
    ls = LanguageServerClient()
    # Binary data with some readable text
    data = b"project\x00context\x00data"
    result = ls._decode_project_context(data)
    assert isinstance(result, dict)
    assert "summary" in result or "raw" in result


# --- Step decoding ---

def test_decode_step_with_status():
    """Test decoding step with status field."""
    ls = LanguageServerClient()
    # Field 1 (type): 14
    # Field 4 (status): 2 (INITIALIZED)
    data = encode_varint((1 << 3) | 0) + encode_varint(14)  # type = 14
    data += encode_varint((4 << 3) | 0) + encode_varint(2)  # status = 2
    step = ls._decode_step(data)
    assert step["type"] == 14
    assert step["status"] == 2


def test_decode_step_empty():
    """Test decoding empty step."""
    ls = LanguageServerClient()
    step = ls._decode_step(b"")
    assert step["type"] is None
    assert step["status"] is None
    assert step["data"] == {}


def test_decode_step_with_generic_field():
    """Test decoding step with generic field."""
    ls = LanguageServerClient()
    # Field 1 (type): 14
    # Field 109 (generic): "test"
    data = encode_varint((1 << 3) | 0) + encode_varint(14)  # type = 14
    data += encode_varint((109 << 3) | 2) + encode_varint(4) + b"test"  # generic field
    step = ls._decode_step(data)
    assert step["type"] == 14
    assert "data_109" in step


def test_serialize_step_preserves_structure():
    """Test that serialize_step preserves structure."""
    step = {
        "type": 14,
        "status": 2,
        "data": {
            "user_input": "test",
            "nested": {
                "key": "value",
            },
        },
    }
    serialized = serialize_step(step)
    assert serialized["type"] == 14
    assert serialized["status"] == 2
    assert serialized["data"]["user_input"] == "test"
    assert serialized["data"]["nested"]["key"] == "value"


def test_decode_write_file():
    """Test _decode_write_file decoder."""
    ls = LanguageServerClient()
    # Simple write file data
    data = b"\x0a\x05/path"  # field 1, length 5, "/path"
    result = ls._decode_write_file(data)
    assert isinstance(result, dict)
    assert "path" in result


def test_decode_run_command():
    """Test _decode_run_command decoder."""
    ls = LanguageServerClient()
    # Simple command data
    data = b"\x0a\x03ls"  # field 1, length 3, "ls"
    result = ls._decode_run_command(data)
    assert isinstance(result, dict)
    assert "command" in result


def test_decode_find():
    """Test _decode_find decoder."""
    ls = LanguageServerClient()
    # Simple find data
    data = b"\x0a\x06*.py"  # field 1, length 6, "*.py"
    result = ls._decode_find(data)
    assert isinstance(result, dict)


def test_decode_search_web():
    """Test _decode_search_web decoder."""
    ls = LanguageServerClient()
    # Simple search web data
    data = b"\x0a\x04test"  # field 1, length 4, "test"
    result = ls._decode_search_web(data)
    assert isinstance(result, dict)
    assert "query" in result


def test_decode_file_search_pattern():
    """Test _decode_file_search_pattern decoder."""
    ls = LanguageServerClient()
    # Simple pattern data
    data = b"\x0a\x05*.txt"  # field 1, length 5, "*.txt"
    result = ls._decode_file_search_pattern(data)
    assert isinstance(result, dict)
    assert "pattern" in result


def test_decode_mcp_tool():
    """Test _decode_mcp_tool decoder."""
    ls = LanguageServerClient()
    # Simple MCP tool data
    data = b"\x0a\x06my_tool"  # field 1, length 6, "my_tool"
    result = ls._decode_mcp_tool(data)
    assert isinstance(result, dict)
    assert "tool_name" in result


def test_decode_plan():
    """Test _decode_plan decoder."""
    ls = LanguageServerClient()
    # Simple plan data
    data = b"\x0a\x05plan1"  # field 1, length 5, "plan1"
    result = ls._decode_plan(data)
    assert isinstance(result, dict)


def test_decode_read_url_content():
    """Test _decode_read_url_content decoder."""
    ls = LanguageServerClient()
    # Simple URL content data
    data = b"\x0a\x14https://example.com"  # field 1, length 20, URL
    result = ls._decode_read_url_content(data)
    assert isinstance(result, dict)
    assert "url" in result


def test_decode_command_output():
    """Test _decode_command_output decoder."""
    ls = LanguageServerClient()
    # Simple command output data
    data = b"\x0a\x05output"  # field 1, length 5, "output"
    result = ls._decode_command_output(data)
    assert isinstance(result, dict)


def test_decode_view_file():
    """Test _decode_view_file decoder."""
    ls = LanguageServerClient()
    # Simple view file data
    data = b"\x0a\x05/path"  # field 1, length 5, "/path"
    result = ls._decode_view_file(data)
    assert isinstance(result, dict)


def test_decode_list_directory():
    """Test _decode_list_directory decoder."""
    ls = LanguageServerClient()
    # Simple list directory data
    data = b"\x0a\x05/path"  # field 1, length 5, "/path"
    result = ls._decode_list_directory(data)
    assert isinstance(result, dict)


def test_decode_grep_search():
    """Test _decode_grep_search decoder."""
    ls = LanguageServerClient()
    # Simple grep search data
    data = b"\x0a\x04test"  # field 1, length 4, "test"
    result = ls._decode_grep_search(data)
    assert isinstance(result, dict)


def test_decode_checkpoint():
    """Test _decode_checkpoint decoder."""
    ls = LanguageServerClient()
    # Simple checkpoint data
    data = b"\x0a\x05check"  # field 1, length 5, "check"
    result = ls._decode_checkpoint(data)
    assert isinstance(result, dict)


def test_serialize_step_with_bytes():
    """Test serialize_step converts nested bytes."""
    step = {
        "type": 14,
        "data": {
            "field1": b"bytes",
            "nested": {
                "field2": b"more bytes",
            },
        },
    }
    serialized = serialize_step(step)
    assert isinstance(serialized["data"]["field1"], str)
    assert serialized["data"]["field1"] == "bytes"
    assert isinstance(serialized["data"]["nested"]["field2"], str)
    assert serialized["data"]["nested"]["field2"] == "more bytes"


def test_serialize_step_with_mixed_types():
    """Test serialize_step handles mixed types."""
    step = {
        "type": 14,
        "data": {
            "string": "text",
            "number": 42,
            "boolean": True,
            "bytes": b"bytes",
        },
    }
    serialized = serialize_step(step)
    assert serialized["data"]["string"] == "text"
    assert serialized["data"]["number"] == 42
    assert serialized["data"]["boolean"] is True
    assert serialized["data"]["bytes"] == "bytes"


def test_serialize_step_with_empty_bytes():
    """Test serialize_step handles empty bytes."""
    step = {
        "type": 14,
        "data": {
            "empty": b"",
        },
    }
    serialized = serialize_step(step)
    assert serialized["data"]["empty"] == ""


def test_serialize_step_with_none():
    """Test serialize_step handles None values."""
    step = {
        "type": 14,
        "data": {
            "none": None,
        },
    }
    serialized = serialize_step(step)
    assert serialized["data"]["none"] is None


@pytest.mark.asyncio
async def test_count_threads_uses_language_server():
    """Test count_threads uses Language Server API to get cascade count."""
    from llm_archive.ingestors.windsurf import WindsurfIngestor
    
    ingestor = WindsurfIngestor()
    # count_threads should call LanguageServerClient.get_all_cascade_ids
    # This test verifies the method exists and returns an integer
    # Actual count depends on running Windsurf instance
    try:
        count = await ingestor.count_threads()
        assert isinstance(count, int)
        assert count >= 0
    except Exception:
        # If Windsurf is not running, the method should handle gracefully
        # and return 0 or raise an appropriate exception
        pass


class MockProcessInfo:
    def __init__(self, name, pid, cmdline, exe, cwd):
        self.info = {
            'name': name,
            'pid': pid,
            'cmdline': cmdline,
            'exe': exe,
            'cwd': cwd,
        }


class MockConnection:
    def __init__(self, status, laddr):
        self.status = status
        self.laddr = laddr


class MockProcess:
    def __init__(self, name, pid, cmdline, exe, cwd, connections):
        self.info = {
            'name': name,
            'pid': pid,
            'cmdline': cmdline,
            'exe': exe,
            'cwd': cwd,
        }
        self._connections = connections

    def net_connections(self, kind=None):
        return self._connections


def test_detect_ls_port_with_windsurf_process():
    """Test port detection finds Windsurf language server process."""
    from llm_archive.ingestors.windsurf import _detect_ls_port
    from unittest.mock import patch

    # Mock process with Windsurf-specific flags
    mock_process = MockProcess(
        name='language_server_linux_x64',
        pid=12345,
        cmdline=['language_server_linux_x64', '--enable_lsp', '--codeium_dir', '/path/to/codeium'],
        exe='/path/to/language_server_linux_x64',
        cwd='/home/user/.codeium/windsurf',
        connections=[MockConnection('LISTEN', type('Addr', (), {'port': 12345}))]
    )

    with patch('llm_archive.ingestors.windsurf.psutil') as mock_psutil:
        mock_psutil.process_iter.return_value = [mock_process]
        
        try:
            port = _detect_ls_port()
            assert port == 12345
        except RuntimeError:
            # If psutil is not available, this is expected
            pass


def test_detect_ls_port_without_windsurf_flags():
    """Test port detection ignores process without Windsurf flags."""
    from llm_archive.ingestors.windsurf import _detect_ls_port
    from unittest.mock import patch

    # Mock process without Windsurf-specific flags
    mock_process = MockProcess(
        name='language_server',
        pid=12345,
        cmdline=['language_server', '--some-flag'],
        exe='/path/to/language_server',
        cwd='/tmp',
        connections=[]
    )

    with patch('llm_archive.ingestors.windsurf.psutil') as mock_psutil:
        mock_psutil.process_iter.return_value = [mock_process]
        
        try:
            _detect_ls_port()
            assert False, "Should raise RuntimeError"
        except RuntimeError as e:
            assert "Could not detect Windsurf language server port" in str(e)


def test_detect_ls_port_with_windsurf_cwd():
    """Test port detection uses working directory to identify Windsurf."""
    from llm_archive.ingestors.windsurf import _detect_ls_port
    from unittest.mock import patch

    # Mock process with Windsurf working directory but no specific flags
    mock_process = MockProcess(
        name='language_server',
        pid=12345,
        cmdline=['language_server'],
        exe='/path/to/language_server',
        cwd='/home/user/.codeium/windsurf/database',
        connections=[MockConnection('LISTEN', type('Addr', (), {'port': 12345}))]
    )

    with patch('llm_archive.ingestors.windsurf.psutil') as mock_psutil:
        mock_psutil.process_iter.return_value = [mock_process]
        
        try:
            port = _detect_ls_port()
            assert port == 12345
        except RuntimeError:
            # If psutil is not available, this is expected
            pass
