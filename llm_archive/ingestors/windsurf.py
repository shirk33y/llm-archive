from __future__ import annotations
import asyncio
import gzip
import json
import re
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.logging import get_logger, retry_async
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

logger = get_logger("windsurf")

try:
    import psutil
except ImportError:
    psutil = None

def _csrf_token_path() -> Path:
    """Get path to CSRF token file."""
    return Path.home() / ".llm-archive" / "auth" / "windsurf.json"


def _save_csrf_token(token: str, hostname: str | None = None) -> None:
    """Save CSRF token and hostname to file."""
    _csrf_token_path().parent.mkdir(parents=True, exist_ok=True)
    data = {"csrf_token": token}
    if hostname:
        data["hostname"] = hostname
    _csrf_token_path().write_text(json.dumps(data))


def _load_csrf_token() -> tuple[str | None, str | None]:
    """Load CSRF token and hostname from file."""
    path = _csrf_token_path()
    if path.exists():
        data = json.loads(path.read_text())
        return data.get("csrf_token"), data.get("hostname")
    return None, None


async def _extract_csrf_token_via_cdp(cdp_port: int = 9222, timeout: int = 30) -> tuple[str | None, str | None]:
    """Extract CSRF token and hostname by intercepting automatic language server requests via CDP."""
    import urllib.request
    import websockets
    
    cdp_url = f"http://127.0.0.1:{cdp_port}/json"
    logger.info(f"Connecting to CDP on port {cdp_port}...")
    
    try:
        with urllib.request.urlopen(cdp_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
            
            page_target = None
            for target in targets:
                if target.get('type') == 'page':
                    page_target = target
                    break
            
            if not page_target:
                logger.error("No page target found in CDP")
                return None, None
            
            ws_url = page_target['webSocketDebuggerUrl']
            logger.info(f"Connecting to CDP WebSocket")
    except Exception as e:
        logger.error(f"Error getting CDP targets: {e}")
        return None, None
    
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.enable",
            "params": {}
        }))
        
        logger.info(f"Listening for automatic language server requests (timeout: {timeout}s)...")
        
        csrf_token = None
        hostname = None
        start_time = asyncio.get_event_loop().time()
        
        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                
                if data.get('method') == 'Network.requestWillBeSent':
                    request_data = data['params']['request']
                    url = request_data['url']
                    headers = request_data.get('headers', {})
                    
                    if 'language_server' in url.lower():
                        logger.info(f"Intercepted request: {url}")
                        # Extract hostname from URL (e.g., http://t.localhost:42361 -> t.localhost)
                        from urllib.parse import urlparse
                        parsed = urlparse(url)
                        hostname = parsed.hostname
                        logger.info(f"Extracted hostname: {hostname}")
                        
                        for key, value in headers.items():
                            if 'csrf' in key.lower():
                                csrf_token = value
                                logger.info(f"Found CSRF token: {csrf_token}")
                                _save_csrf_token(csrf_token, hostname)
                                return csrf_token, hostname
            
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error processing message: {e}")
        
        logger.error("No language server request detected within timeout")
        return None, None


def _detect_ls_port() -> int:
    """Detect the local language server port by finding the process and its listening ports."""
    # Try to find language server process and its ports using psutil
    if psutil:
        try:
            for proc in psutil.process_iter(['name', 'pid', 'cmdline', 'exe', 'cwd']):
                try:
                    # Check if this is the Windsurf language server by examining:
                    # 1. Binary name contains "language_server"
                    # 2. Command line has Windsurf-specific flags
                    # 3. Working directory contains Windsurf-specific paths
                    name = proc.info.get('name', '')
                    exe = proc.info.get('exe', '')
                    cmdline = proc.info.get('cmdline', [])
                    cwd = proc.info.get('cwd', '')
                    
                    # Check binary name or path
                    is_language_server = 'language_server' in name.lower() or (exe and 'language_server' in exe.lower())
                    
                    # Check for Windsurf-specific flags in command line
                    # Using most specific flags: --windsurf_version, --codeium_dir
                    has_windsurf_flags = False
                    if cmdline:
                        cmdline_str = ' '.join(cmdline).lower()
                        has_windsurf_flags = any(flag in cmdline_str for flag in [
                            '--windsurf_version',
                            '--codeium_dir'
                        ])
                    
                    # Check working directory for Windsurf-specific paths
                    has_windsurf_cwd = False
                    if cwd:
                        cwd_lower = cwd.lower()
                        has_windsurf_cwd = 'windsurf' in cwd_lower or 'codeium' in cwd_lower
                    
                    if is_language_server and (has_windsurf_flags or has_windsurf_cwd):
                        # Found Windsurf language server process, check its listening ports
                        ports = []
                        for conn in proc.net_connections(kind='inet'):
                            if conn.status == 'LISTEN' and conn.laddr:
                                ports.append(conn.laddr.port)
                        # Return the first port (API port)
                        if ports:
                            return ports[0]
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
    
    # If psutil not available or process not found, raise error
    raise RuntimeError(
        "Could not detect Windsurf language server port. "
        "Please ensure Windsurf is running with the language server enabled."
    )


# Protobuf encoding/decoding
def encode_varint(value: int) -> bytes:
    """Encode a varint (variable-length integer)"""
    result = bytes()
    while value > 0x7F:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    result += bytes([value])
    return result


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a varint from data at offset, return (value, new_offset)"""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            return (0, offset)
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return (result, offset)


def decode_field(data: bytes, offset: int) -> tuple[int, int, bytes | int | None, int]:
    """Decode a protobuf field, return (field_number, wire_type, value, new_offset)"""
    if offset >= len(data):
        return (0, 0, None, offset)
    
    tag, offset = decode_varint(data, offset)
    field_number = tag >> 3
    wire_type = tag & 0x7
    
    if wire_type == 0:  # varint
        value, offset = decode_varint(data, offset)
    elif wire_type == 1:  # 64-bit
        value = struct.unpack("<Q", data[offset:offset+8])[0]
        offset += 8
    elif wire_type == 2:  # length-delimited
        length, offset = decode_varint(data, offset)
        value = data[offset:offset+length]
        offset += length
    elif wire_type == 5:  # 32-bit
        value = struct.unpack("<I", data[offset:offset+4])[0]
        offset += 4
    else:
        value = None
    
    return (field_number, wire_type, value, offset)


def serialize_step(step: dict) -> dict:
    """Convert bytes in step dict to strings for JSON serialization"""
    def serialize_value(val):
        if isinstance(val, bytes):
            return val.decode('utf-8', errors='replace')
        elif isinstance(val, dict):
            return {k: serialize_value(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [serialize_item(v) for v in val]
        return val
    
    def serialize_item(val):
        if isinstance(val, bytes):
            return val.decode('utf-8', errors='replace')
        elif isinstance(val, dict):
            return {k: serialize_item(v) for k, v in val.items()}
        return val
    
    return {k: serialize_value(v) for k, v in step.items()}


def encode_get_cascade_request(cascade_id: str) -> bytes:
    """Encode GetCascadeTrajectoryRequest protobuf message"""
    field_number = 1
    wire_type = 2  # length-delimited
    tag = (field_number << 3) | wire_type
    cascade_id_bytes = cascade_id.encode('utf-8')
    message = bytes()
    message += encode_varint(tag)
    message += encode_varint(len(cascade_id_bytes))
    message += cascade_id_bytes
    return message


class LanguageServerClient:
    """Client for Windsurf language server API"""
    
    def __init__(self, port: int | None = None):
        self._port = port
        self._base = None
        self._hostname = None
    
    @property
    def port(self) -> int:
        if self._port is None:
            self._port = _detect_ls_port()
        return self._port
    
    @property
    def base(self) -> str:
        if self._base is None:
            token, hostname = _load_csrf_token()
            if hostname:
                self._base = f"http://{hostname}:{self.port}"
            else:
                # Will be set by _get_csrf_token after CDP extraction
                self._base = f"http://127.0.0.1:{self.port}"
        return self._base
    
    async def _get_csrf_token(self) -> str:
        """Get CSRF token from running Windsurf with automatic extraction."""
        # Try to load from file first
        token, hostname = _load_csrf_token()
        if token and hostname:
            self._hostname = hostname
            self._base = f"http://{hostname}:{self.port}"
            return token
        
        # Try to extract via CDP
        logger.info("CSRF token or hostname not found in file, attempting CDP extraction...")
        token, hostname = await _extract_csrf_token_via_cdp()
        if token and hostname:
            self._hostname = hostname
            self._base = f"http://{hostname}:{self.port}"
            return token
        
        raise RuntimeError(
            "Could not extract CSRF token or hostname. Ensure Windsurf is running with CDP enabled "
            "(windsurf --remote-debugging-port=9222) and try again."
        )
    
    async def get_trajectory(self, cascade_id: str) -> dict | None:
        """Get trajectory data for a cascade_id with retry and hostname re-extraction"""
        for attempt in range(3):
            try:
                url = f"{self.base}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"
                
                headers = {
                    "x-codeium-csrf-token": await self._get_csrf_token(),
                    "connect-protocol-version": "1",
                    "content-type": "application/proto",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Windsurf/1.110.1 Chrome/142.0.7444.265 Electron/39.6.0 Safari/537.36"
                }
                
                protobuf_message = encode_get_cascade_request(cascade_id)
                
                req = urllib.request.Request(url, data=protobuf_message, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status != 200:
                        return None
                    
                    content = response.read()
                    
                    # Decompress if gzipped
                    if response.headers.get('Content-Encoding') == 'gzip':
                        content = gzip.decompress(content)
                    
                    # Decode the trajectory
                    return self._decode_trajectory(content)
            
            except Exception as e:
                if attempt == 2:
                    raise
                
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Re-extracting hostname...")
                # Re-extract hostname via CDP
                token, hostname = await _extract_csrf_token_via_cdp()
                if hostname:
                    self._hostname = hostname
                    self._base = f"http://{hostname}:{self.port}"
                    logger.info(f"Re-extracted hostname: {hostname}")
                await asyncio.sleep(1)
        
        return None
    
    def _decode_trajectory(self, data: bytes) -> dict:
        """Decode GetCascadeTrajectoryResponse"""
        offset = 0
        result = {
            "trajectory_id": None,
            "cascade_id": None,
            "step_count": 0,
            "steps": []
        }
        
        while offset < len(data):
            field_number, wire_type, value, offset = decode_field(data, offset)
            
            if field_number == 1:  # trajectory (CortexTrajectory)
                trajectory_offset = 0
                while trajectory_offset < len(value):
                    fn, wt, val, trajectory_offset = decode_field(value, trajectory_offset)
                    
                    if fn == 1:  # trajectory_id
                        result["trajectory_id"] = val.decode('utf-8', errors='replace')
                    elif fn == 6:  # cascade_id
                        result["cascade_id"] = val.decode('utf-8', errors='replace')
                    elif fn == 2:  # steps (repeated CortexTrajectoryStep)
                        step = self._decode_step(val)
                        result["step_count"] += 1
                        result["steps"].append(step)
            
            elif field_number == 3:  # num_total_steps
                result["step_count"] = value
        
        return result
    
    def _decode_metadata_timestamp(self, data: bytes) -> int | None:
        """Decode CortexStepMetadata to extract created_at timestamp (field 1)."""
        if not data:
            return None
        offset = 0
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # created_at (Timestamp message)
                # Timestamp has fields: seconds (field 1), nanos (field 2)
                if isinstance(val, bytes):
                    ts_offset = 0
                    seconds = 0
                    nanos = 0
                    while ts_offset < len(val):
                        ts_fn, ts_wt, ts_val, ts_offset = decode_field(val, ts_offset)
                        if ts_fn == 1:  # seconds
                            seconds = ts_val
                        elif ts_fn == 2:  # nanos
                            nanos = ts_val
                    # Convert to milliseconds
                    return int(seconds * 1000 + nanos / 1000000)
                elif isinstance(val, int):
                    # Might be just seconds in some cases
                    return int(val * 1000)
        return None
    
    def _decode_step(self, data: bytes) -> dict:
        """Decode CortexTrajectoryStep"""
        offset = 0
        step = {"type": None, "status": None, "data": {}, "timestamp_ms": None}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # type (enum)
                step["type"] = val
            elif fn == 4:  # status (enum)
                step["status"] = val
            elif fn == 5:  # metadata (message) - CortexStepMetadata
                step["timestamp_ms"] = self._decode_metadata_timestamp(val)
            elif fn == 13:  # grep_search (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["grep_search"] = self._decode_grep_search(val)
            elif fn == 14:  # view_file (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["view_file"] = self._decode_view_file(val)
            elif fn == 15:  # list_directory (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["list_directory"] = self._decode_list_directory(val)
            elif fn == 19:  # user_input (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["user_input"] = self._decode_user_input(val)
            elif fn == 20:  # planner_response (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["planner_response"] = self._decode_planner_response(val)
            elif fn == 23:  # write_to_file (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["write_file"] = self._decode_write_file(val)
            elif fn == 28:  # run_command (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["run_command"] = self._decode_run_command(val)
            elif fn == 29:  # related_files (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["related_files"] = val.decode('utf-8', errors='replace')
            elif fn == 30:  # checkpoint (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["checkpoint"] = self._decode_checkpoint(val)
            elif fn == 24:  # error_message (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["error_message"] = self._decode_error_message(val)
            elif fn == 34:  # find (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["find"] = self._decode_find(val)
            elif fn == 37:  # command_output (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["command_output"] = self._decode_command_output(val)
            elif fn == 40:  # read_url_content (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["read_url_content"] = self._decode_read_url_content(val)
            elif fn == 47:  # mcp_tool (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["mcp_tool"] = self._decode_mcp_tool(val)
            elif fn == 62:  # tool_name (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["tool_name"] = val.decode('utf-8', errors='replace')
            elif fn == 63:  # tool_name_with_state (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["tool_name_with_state"] = val.decode('utf-8', errors='replace')
            elif fn == 6:  # context_memory (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["context_memory"] = self._decode_context_memory(val)
            elif fn == 38:  # project_context (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["project_context"] = self._decode_project_context(val)
            elif fn == 43:  # empty_field (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["empty_field"] = val.decode('utf-8', errors='replace')
            elif fn == 56:  # url_reference (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["url_reference"] = self._decode_url_reference(val)
            elif fn == 10:  # file_content_edit (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["file_content_edit"] = self._decode_file_content_edit(val)
            elif fn == 41:  # parsed_url_content (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["parsed_url_content"] = self._decode_parsed_url_content(val)
            elif fn == 105:  # file_search_pattern (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["file_search_pattern"] = self._decode_file_search_pattern(val)
            elif fn == 87:  # plan (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["plan"] = self._decode_plan(val)
            elif fn == 97:  # file_uri (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["file_uri"] = val.decode('utf-8', errors='replace')
            elif fn == 101:  # grep_result (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["grep_result"] = val.decode('utf-8', errors='replace')
            elif fn == 115:  # ask_user_question (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["ask_user_question"] = val.decode('utf-8', errors='replace')
            elif fn == 42:  # search_web (oneof: step)
                if isinstance(val, bytes):
                    step["data"]["search_web"] = self._decode_search_web(val)
            elif fn in [109]:  # Other/rare step data fields - keep generic
                if isinstance(val, bytes):
                    step[f"data_{fn}"] = val.decode('utf-8', errors='replace')
            else:
                # Store unknown fields for debugging
                if fn not in [1, 2, 3, 4, 5]:  # Skip common non-step fields
                    step[f"field_{fn}"] = val
        
        return step
    
    def _decode_user_input(self, data: bytes) -> str:
        """Decode CortexStepUserInput"""
        offset = 0
        result = ""
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # query
                if isinstance(val, bytes):
                    result = val.decode('utf-8', errors='replace')
            elif fn == 2:  # user_response
                if isinstance(val, bytes):
                    result = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_planner_response(self, data: bytes) -> str:
        """Decode CortexStepPlannerResponse"""
        offset = 0
        result = ""
        thinking = ""
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # response
                if isinstance(val, bytes):
                    result = val.decode('utf-8', errors='replace')
            elif fn == 8:  # modified_response
                if isinstance(val, bytes):
                    result = val.decode('utf-8', errors='replace')
            elif fn == 3:  # thinking
                if isinstance(val, bytes):
                    thinking = val.decode('utf-8', errors='replace')
        
        if thinking:
            return f"[Thinking]\n{thinking}\n\n{result}"
        return result
    
    def _decode_write_file(self, data: bytes) -> dict:
        """Decode WriteToFile message"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # path
                if isinstance(val, bytes):
                    result["path"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # content
                if isinstance(val, bytes):
                    result["content"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_run_command(self, data: bytes) -> dict:
        """Decode RunCommand message"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # command
                if isinstance(val, bytes):
                    result["command"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # args (repeated)
                if isinstance(val, bytes):
                    result["args"] = val.decode('utf-8', errors='replace')
            elif fn == 3:  # stdout
                if isinstance(val, bytes):
                    result["stdout"] = val.decode('utf-8', errors='replace')
            elif fn == 4:  # exit_code
                result["exit_code"] = val
        
        return result
    
    def _decode_view_file(self, data: bytes) -> dict:
        """Decode CortexStepViewFile"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # absolute_path_uri
                if isinstance(val, bytes):
                    result["path"] = val.decode('utf-8', errors='replace')
            elif fn == 4:  # content
                if isinstance(val, bytes):
                    result["content"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # start_line
                result["start_line"] = val
            elif fn == 3:  # end_line
                result["end_line"] = val
        
        return result
    
    def _decode_list_directory(self, data: bytes) -> dict:
        """Decode CortexStepListDirectory"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # directory_path_uri
                if isinstance(val, bytes):
                    result["path"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # children (repeated strings)
                if isinstance(val, bytes):
                    result["children"] = val.decode('utf-8', errors='replace')
            elif fn == 4:  # dir_not_found
                result["not_found"] = val
        
        return result
    
    def _decode_grep_search(self, data: bytes) -> dict:
        """Decode CortexStepGrepSearch"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # query
                if isinstance(val, bytes):
                    result["query"] = val.decode('utf-8', errors='replace')
            elif fn == 11:  # search_path_uri
                if isinstance(val, bytes):
                    result["path"] = val.decode('utf-8', errors='replace')
            elif fn == 7:  # total_results
                result["total"] = val
        
        return result
    
    def _decode_search_web(self, data: bytes) -> dict:
        """Decode SearchWeb"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # query
                if isinstance(val, bytes):
                    result["query"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # results
                if isinstance(val, bytes):
                    result["results"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_checkpoint(self, data: bytes) -> dict:
        """Decode Checkpoint"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            
            if fn == 1:  # message
                if isinstance(val, bytes):
                    result["message"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # checkpoint_id
                if isinstance(val, bytes):
                    result["checkpoint_id"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_error_message(self, data: bytes) -> str:
        """Decode error message (field 24) - protobuf encoded"""
        # Field 24 contains nested protobuf with error text
        # Extract readable text by decoding as UTF-8 and removing control characters
        text = data.decode('utf-8', errors='replace')
        # Remove common protobuf control characters and extract readable text
        # The error text is usually the longest readable segment
        # Extract sequences of printable ASCII text longer than 20 chars
        readable = re.findall(r'[ -~]{20,}', text)
        if readable:
            return ' '.join(readable)
        return text  # Return full text
    
    def _decode_find(self, data: bytes) -> dict:
        """Decode find/file search results"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # pattern
                if isinstance(val, bytes):
                    result["pattern"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # results
                if isinstance(val, bytes):
                    result["results"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_command_output(self, data: bytes) -> dict:
        """Decode command output"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # stdout
                if isinstance(val, bytes):
                    result["stdout"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # stderr
                if isinstance(val, bytes):
                    result["stderr"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_read_url_content(self, data: bytes) -> dict:
        """Decode read URL content"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # url
                if isinstance(val, bytes):
                    result["url"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # content
                if isinstance(val, bytes):
                    result["content"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_mcp_tool(self, data: bytes) -> dict:
        """Decode MCP tool call"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # tool_name
                if isinstance(val, bytes):
                    result["tool_name"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # call_id
                if isinstance(val, bytes):
                    result["call_id"] = val.decode('utf-8', errors='replace')
            elif fn == 3:  # method
                if isinstance(val, bytes):
                    result["method"] = val.decode('utf-8', errors='replace')
            elif fn == 4:  # args
                if isinstance(val, bytes):
                    result["args"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_plan(self, data: bytes) -> dict:
        """Decode plan data"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # plan_id
                if isinstance(val, bytes):
                    result["plan_id"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # plans
                if isinstance(val, bytes):
                    result["plans"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_file_content_edit(self, data: bytes) -> str:
        """Decode file content for editing (field 10)"""
        # Field 10 contains shell scripts, .desktop files, etc.
        # Extract readable text
        text = data.decode('utf-8', errors='replace')
        # Extract sequences of printable text
        readable = re.findall(r'[ -~]{10,}', text)
        if readable:
            return '\n'.join(readable)
        return text
    
    def _decode_parsed_url_content(self, data: bytes) -> str:
        """Decode parsed URL content (field 41)"""
        # Field 41 contains markdown-formatted URL content with links
        text = data.decode('utf-8', errors='replace')
        # Extract readable text, preserving markdown links
        readable = re.findall(r'[ -~]{10,}', text)
        if readable:
            return ' '.join(readable)
        return text
    
    def _decode_file_search_pattern(self, data: bytes) -> dict:
        """Decode file search pattern (field 105)"""
        offset = 0
        result = {}
        
        while offset < len(data):
            fn, wt, val, offset = decode_field(data, offset)
            if fn == 1:  # pattern
                if isinstance(val, bytes):
                    result["pattern"] = val.decode('utf-8', errors='replace')
            elif fn == 2:  # results
                if isinstance(val, bytes):
                    result["results"] = val.decode('utf-8', errors='replace')
        
        return result
    
    def _decode_context_memory(self, data: bytes) -> dict:
        """Decode context/memory state (field 6) - binary protobuf with UUIDs"""
        # Field 6 contains binary protobuf with UUIDs, timestamps, encrypted content
        # Extract any readable text
        text = data.decode('utf-8', errors='replace')
        readable = re.findall(r'[ -~]{10,}', text)
        if readable:
            return {"summary": ' '.join(readable)}
        return {"raw": text}
    
    def _decode_project_context(self, data: bytes) -> dict:
        """Decode project context (field 38) - binary protobuf with UUIDs and task descriptions"""
        # Field 38 contains UUIDs, task summaries, project context
        text = data.decode('utf-8', errors='replace')
        readable = re.findall(r'[ -~]{10,}', text)
        if readable:
            return {"summary": ' '.join(readable)}
        return {"raw": text}
    
    def _decode_url_reference(self, data: bytes) -> str:
        """Decode URL reference (field 56)"""
        # Field 56 contains URLs or minimal data
        text = data.decode('utf-8', errors='replace')
        # Extract URLs
        urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', text)
        if urls:
            return urls[0]
        return text
    
    def get_all_cascade_ids(self) -> list[str]:
        """Get all cascade_ids from .pb files"""
        cascade_dir = Path.home() / ".codeium/windsurf/cascade"
        if not cascade_dir.exists():
            return []
        
        cascade_ids = []
        for pb_file in cascade_dir.glob("*.pb"):
            cascade_id = pb_file.stem
            cascade_ids.append(cascade_id)
        
        return cascade_ids


class WindsurfIngestor(BaseIngestor):
    source_id = "windsurf"

    def __init__(self):
        pass

    async def init(self, **kwargs) -> None:
        pass

    async def requires_auth(self) -> bool:
        return False

    async def count_threads(self, since: int | None = None) -> int:
        """Return approximate count of available sessions."""
        # Use language server API
        try:
            ls = LanguageServerClient()
            cascade_ids = ls.get_all_cascade_ids()
            return len(cascade_ids)
        except Exception:
            return 0

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        # Use language server API for historical .pb files by default
        async for thread in self.threads_from_language_server(since=since):
            yield thread

    async def threads_from_language_server(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        """Fetch historical conversations from language server API"""
        ls = LanguageServerClient()
        cascade_ids = ls.get_all_cascade_ids()
        
        logger.info(f"Found {len(cascade_ids)} cascade files")
        
        for cascade_id in cascade_ids:
            logger.debug(f"Fetching conversation for {cascade_id}")
            trajectory = await ls.get_trajectory(cascade_id)
            
            if not trajectory:
                logger.warning(f"Failed to fetch {cascade_id}")
                continue
            
            logger.debug(f"Got conversation with {trajectory['step_count']} steps")
            
            # Convert decoded steps to messages
            thread = self._convert_to_thread(trajectory, cascade_id)
            
            if since and thread.updated_at and thread.updated_at < since:
                continue
            
            yield thread
    
    def _convert_to_thread(self, trajectory: dict, cascade_id: str) -> IngestedThread:
        """Convert decoded trajectory to IngestedThread"""
        traj_id = trajectory.get("trajectory_id", cascade_id)
        steps = trajectory.get("steps", [])
        
        messages: list[IngestedMessage] = []
        
        for i, step in enumerate(steps):
            step_type = step.get("type")
            data = step.get("data", {})
            timestamp = step.get("timestamp_ms")
            
            # Map step types to message roles (based on enum values from beautified code)
            # 14 = CORTEX_STEP_TYPE_USER_INPUT
            # 15 = CORTEX_STEP_TYPE_PLANNER_RESPONSE
            # 23 = CORTEX_STEP_TYPE_WRITE_TO_FILE
            # 28 = CORTEX_STEP_TYPE_RUN_COMMAND
            # 8 = CORTEX_STEP_TYPE_VIEW_FILE
            # 9 = CORTEX_STEP_TYPE_LIST_DIRECTORY
            # 7 = CORTEX_STEP_TYPE_GREP_SEARCH
            # etc.
            
            if step_type == 14:  # USER_INPUT
                user_input = data.get("user_input", "")
                if user_input:
                    parts = [IngestedPart(kind="text", text=user_input)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="user",
                        content=user_input,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 15:  # PLANNER_RESPONSE
                planner_response = data.get("planner_response", "")
                if planner_response:
                    parts = [IngestedPart(kind="text", text=planner_response)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="assistant",
                        content=planner_response,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 28:  # RUN_COMMAND
                cmd_data = data.get("run_command", {})
                command = cmd_data.get("command", "")
                if command:
                    stdout = cmd_data.get("stdout", "")
                    content = f"[Command: {command}]"
                    if stdout:
                        content += f"\nOutput:\n{stdout}"
                    parts = [IngestedPart(kind="tool_call", text=content, data=cmd_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 23:  # WRITE_TO_FILE
                write_data = data.get("write_file", {})
                path = write_data.get("path", "")
                if path:
                    content = f"[Write file: {path}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=write_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 8:  # VIEW_FILE
                view_data = data.get("view_file", {})
                path = view_data.get("path", "")
                if path:
                    content = f"[View file: {path}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=view_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 9:  # LIST_DIRECTORY
                list_data = data.get("list_directory", {})
                path = list_data.get("path", "")
                if path:
                    content = f"[List directory: {path}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=list_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 7:  # GREP_SEARCH
                grep_data = data.get("grep_search", {})
                query = grep_data.get("query", "")
                if query:
                    content = f"[Grep search: {query}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=grep_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 33:  # SEARCH_WEB
                search_data = data.get("search_web", {})
                query = search_data.get("query", "")
                results = search_data.get("results", "")
                if query:
                    content = f"[Web search: {query}]"
                    if results:
                        content += f"\n{results}"
                    parts = [IngestedPart(kind="tool_call", text=content, data=search_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif step_type == 23:  # CHECKPOINT
                checkpoint_data = data.get("checkpoint", {})
                message = checkpoint_data.get("message", "")
                if message:
                    content = f"[Checkpoint: {message}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=checkpoint_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "error_message" in data:
                error_msg = data["error_message"]
                if error_msg:
                    content = f"[Error: {error_msg}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data={"error_message": error_msg})]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "find" in data:
                find_data = data["find"]
                pattern = find_data.get("pattern", "")
                if pattern:
                    content = f"[Find: {pattern}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=find_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "command_output" in data:
                cmd_output = data["command_output"]
                stdout = cmd_output.get("stdout", "")
                if stdout:
                    content = f"[Command output]\n{stdout}"
                    parts = [IngestedPart(kind="tool_call", text=content, data=cmd_output)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "read_url_content" in data:
                url_data = data["read_url_content"]
                url = url_data.get("url", "")
                if url:
                    content = f"[Read URL: {url}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=url_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "mcp_tool" in data:
                mcp_data = data["mcp_tool"]
                tool_name = mcp_data.get("tool_name", "")
                if tool_name:
                    content = f"[MCP tool: {tool_name}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=mcp_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "plan" in data:
                plan_data = data["plan"]
                plans = plan_data.get("plans", "")
                if plans:
                    content = f"[Plan]\n{plans}"
                    parts = [IngestedPart(kind="tool_call", text=content, data=plan_data)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "ask_user_question" in data:
                question = data["ask_user_question"]
                if question:
                    content = f"[User question: {question}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data={"question": question})]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "file_content_edit" in data:
                file_content = data["file_content_edit"]
                if file_content:
                    content = f"[File content edit]\n{file_content}"
                    parts = [IngestedPart(kind="tool_call", text=content, data={"file_content": file_content})]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "parsed_url_content" in data:
                url_content = data["parsed_url_content"]
                if url_content:
                    content = f"[Parsed URL content]\n{url_content}"
                    parts = [IngestedPart(kind="tool_call", text=content, data={"url_content": url_content})]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "file_search_pattern" in data:
                search_pattern = data["file_search_pattern"]
                pattern = search_pattern.get("pattern", "")
                if pattern:
                    content = f"[File search pattern: {pattern}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=search_pattern)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "context_memory" in data:
                context = data["context_memory"]
                summary = context.get("summary", "")
                if summary:
                    content = f"[Context memory: {summary}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=context)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "project_context" in data:
                project_ctx = data["project_context"]
                summary = project_ctx.get("summary", "")
                if summary:
                    content = f"[Project context: {summary}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data=project_ctx)]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
            
            elif "url_reference" in data:
                url_ref = data["url_reference"]
                if url_ref:
                    content = f"[URL reference: {url_ref}]"
                    parts = [IngestedPart(kind="tool_call", text=content, data={"url": url_ref})]
                    messages.append(IngestedMessage(
                        id=f"windsurf:ls:{traj_id}:{i}",
                        thread_id=f"windsurf:ls:{traj_id}",
                        role="tool",
                        content=content,
                        created_at=timestamp,
                        metadata={},
                        parts=parts,
                        raw=serialize_step(step),
                    ))
        
        # Generate title from first user message
        title = f"Cascade {cascade_id[:8]} ({len(messages)} messages)"
        if messages:
            first_user = next((m for m in messages if m.role == "user"), None)
            if first_user:
                title = first_user.content[:80].split("\n")[0].strip()
        
        # Use first message's timestamp for thread
        first_timestamp = messages[0].created_at if messages else None
        
        return IngestedThread(
            id=f"windsurf:ls:{traj_id}",
            source_id="windsurf",
            title=title,
            created_at=first_timestamp,
            updated_at=first_timestamp,
            messages=messages,
        )
