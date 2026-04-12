from __future__ import annotations
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import AsyncIterator

from llm_archive.ingestors.base import BaseIngestor
from llm_archive.schema import IngestedMessage, IngestedPart, IngestedThread

try:
    import websocket
except ImportError:
    websocket = None

CDP_PORT = 9222
CDP_BASE = f"http://localhost:{CDP_PORT}"


def _is_cdp_running() -> bool:
    try:
        urllib.request.urlopen(f"{CDP_BASE}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


def _get_main_ws() -> str:
    data = json.loads(urllib.request.urlopen(f"{CDP_BASE}/json/list").read())
    pages = [t for t in data if t["type"] == "page"]
    if not pages:
        raise RuntimeError("No page target in CDP")
    return pages[0]["webSocketDebuggerUrl"]


class CDPClient:
    def __init__(self, ws_url: str, timeout: int = 15):
        if websocket is None:
            raise RuntimeError("websocket-client required: uv add websocket-client")
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: int = 15) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            try:
                r = json.loads(self.ws.recv())
                if r.get("id") == self._id:
                    return r.get("result", {})
            except websocket.WebSocketTimeoutException:
                return {}

    def eval(self, expr: str, await_promise: bool = False, timeout: int = 15):
        params = {"expression": expr, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        r = self.call("Runtime.evaluate", params, timeout=timeout)
        res = r.get("result", {})
        if res.get("type") == "object" and res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "JS error"))
        return res.get("value")

    def close(self):
        self.ws.close()


def _store_trajectory(cdp: CDPClient) -> int:
    count = cdp.eval("""
    (function() {
        const el = document.getElementById('windsurf.cascadePanel');
        if (!el) return -1;
        const containerKey = Object.keys(el).find(k => k.startsWith('__reactContainer'));
        if (!containerKey) return -2;
        let root = el[containerKey];
        let trajectory = null;
        let visited = new WeakSet();

        function traverse(fiber, depth) {
            if (!fiber || depth > 150 || trajectory) return;
            if (typeof fiber !== 'object') return;
            try { if (visited.has(fiber)) return; visited.add(fiber); } catch(e) { return; }
            let props = fiber.memoizedProps || fiber.pendingProps;
            if (props && props.trajectory && props.trajectory.steps && props.trajectory.steps.length > 0) {
                trajectory = props.trajectory;
                return;
            }
            traverse(fiber.child, depth+1);
            traverse(fiber.sibling, depth+1);
        }

        traverse(root, 0);
        window.__cascadeTrajectory = trajectory;
        return trajectory ? trajectory.steps.length : 0;
    })()
    """)
    return count or 0


def _extract_conversation(cdp: CDPClient) -> dict | None:
    raw = cdp.eval("""
    (function() {
        let traj = window.__cascadeTrajectory;
        if (!traj) return null;

        let turns = [];
        for (let step of traj.steps) {
            let case_ = step.step && step.step.case;
            let val = step.step && step.step.value;
            let meta = step.metadata || {};

            if (case_ === 'userInput' && val) {
                let text = val.userResponse || (val.items && val.items.map(i=>i.text||'').join('')) || '';
                if (text) turns.push({role: 'user', text: text, at: meta.createdAt});
            }
            else if (case_ === 'plannerResponse' && val) {
                if (val.response) {
                    turns.push({
                        role: 'assistant',
                        text: val.response,
                        thinking: val.thinking || null,
                        at: meta.createdAt
                    });
                }
            }
            else if (case_ === 'runCommand' && val) {
                turns.push({
                    role: 'tool',
                    command: val.command || '',
                    args: val.args || [],
                    stdout: val.stdout || val.stdoutBuffer || '',
                    exitCode: val.exitCode,
                    at: meta.createdAt
                });
            }
            else if (case_ === 'writeFile' && val) {
                turns.push({
                    role: 'tool',
                    tool: 'write_file',
                    path: val.path || val.filePath || '',
                    content: val.content || '',
                    at: meta.createdAt
                });
            }
            else if (case_ === 'readFile' && val) {
                turns.push({role: 'tool', tool: 'read_file', path: val.path || val.filePath || '', at: meta.createdAt});
            }
            else if (case_ === 'todoList' && val) {
                turns.push({role: 'tool', tool: 'todo_list', todos: (val.todos || []).map(t => t.content), at: meta.createdAt});
            }
            else if (case_ === 'checkpoint' && val) {
                turns.push({role: 'checkpoint', intent: val.userIntent || '', at: meta.createdAt});
            }
        }

        return JSON.stringify({
            trajectoryId: traj.trajectoryId,
            turns: turns
        });
    })()
    """)
    if not raw:
        return None
    return json.loads(raw)


def _parse_timestamp(ts: str | None) -> int | None:
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _convert_to_thread(conv: dict, title: str = "") -> IngestedThread:
    traj_id = conv.get("trajectoryId", "unknown")
    turns = conv.get("turns", [])

    messages: list[IngestedMessage] = []
    created_at = None
    updated_at = None

    for i, turn in enumerate(turns):
        role = turn.get("role")
        ts = _parse_timestamp(turn.get("at"))

        if ts:
            if created_at is None:
                created_at = ts
            updated_at = ts

        parts: list[IngestedPart] = []
        content = ""

        if role == "user":
            content = turn.get("text", "")
            parts.append(IngestedPart(kind="text", text=content))

        elif role == "assistant":
            thinking = turn.get("thinking")
            text = turn.get("text", "")
            if thinking:
                parts.append(IngestedPart(kind="thinking", text=thinking, visible=False))
                content = f"[Thinking]\n{thinking}\n\n{text}"
            else:
                content = text
            parts.append(IngestedPart(kind="text", text=text))

        elif role == "tool":
            tool = turn.get("tool", "")
            if tool == "write_file":
                path = turn.get("path", "")
                content = f"[Write file: {path}]"
                parts.append(IngestedPart(kind="tool_call", text=content, data={"path": path}))
            elif tool == "read_file":
                path = turn.get("path", "")
                content = f"[Read file: {path}]"
                parts.append(IngestedPart(kind="tool_call", text=content, data={"path": path}))
            elif tool == "todo_list":
                todos = turn.get("todos", [])
                content = "[TODO list]\n" + "\n".join(f"  - {t}" for t in todos)
                parts.append(IngestedPart(kind="tool_call", text=content, data={"todos": todos}))
            elif turn.get("command"):
                cmd = " ".join([turn.get("command", "")] + (turn.get("args") or []))
                stdout = turn.get("stdout", "")
                content = f"[Command: {cmd}]"
                if stdout:
                    content += f"\nOutput:\n{stdout[:500]}"
                parts.append(IngestedPart(kind="tool_call", text=content, data={"command": cmd, "stdout": stdout}))
            else:
                content = str(turn)
                parts.append(IngestedPart(kind="text", text=content))

        elif role == "checkpoint":
            intent = turn.get("intent", "")
            content = f"[Checkpoint: {intent}]"
            parts.append(IngestedPart(kind="system", text=content, visible=False))

        else:
            content = str(turn)
            parts.append(IngestedPart(kind="text", text=content))

        messages.append(IngestedMessage(
            id=f"windsurf:{traj_id}:{i}",
            thread_id=f"windsurf:{traj_id}",
            role=role if role in ("user", "assistant", "system", "tool") else "system",
            content=content,
            created_at=ts,
            metadata={},
            parts=parts,
            raw=turn,
        ))

    if not title and messages:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].split("\n")[0].strip()

    return IngestedThread(
        id=f"windsurf:{traj_id}",
        source_id="windsurf",
        title=title or traj_id,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
    )


def _restart_windsurf_with_cdp(cdp_port: int = CDP_PORT) -> bool:
    """Kill existing Windsurf and restart with CDP enabled."""
    print("Restarting Windsurf with CDP...", file=__import__("sys").stderr)

    # Kill existing windsurf processes
    subprocess.run(["pkill", "-f", "windsurf"], capture_output=True)
    time.sleep(2)

    # Try different launch methods
    launch_cmds = [
        # Direct binary
        ["windsurf", f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*"],
        # Flatpak
        ["flatpak", "run", "com.codeium.Windsurf", f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*"],
        # Distrobox container
        ["distrobox", "enter", "windsurf", "--", "windsurf", f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*"],
    ]

    for cmd in launch_cmds:
        try:
            # Try to run the command
            result = subprocess.run(cmd[:1] + ["--version"], capture_output=True, timeout=5)
            if result.returncode == 0 or b"windsurf" in result.stdout.lower():
                # Command exists, launch Windsurf
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                break
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    # Wait for CDP to become available
    cdp_base = f"http://localhost:{cdp_port}"
    for i in range(30):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"{cdp_base}/json/version", timeout=2).read()
            print(f"CDP ready on port {cdp_port}.", file=__import__("sys").stderr)
            time.sleep(2)  # Extra wait for workbench to load
            return True
        except Exception:
            pass

    print(f"ERROR: CDP did not start on port {cdp_port} within 30s", file=__import__("sys").stderr)
    return False


class WindsurfIngestor(BaseIngestor):
    source_id = "windsurf"

    def __init__(self, cdp_port: int = CDP_PORT, auto_restart: bool = False):
        self.cdp_port = cdp_port
        self.cdp_base = f"http://localhost:{cdp_port}"
        self.auto_restart = auto_restart

    async def requires_auth(self) -> bool:
        return False

    async def init(self, **kwargs) -> None:
        if "cdp_port" in kwargs:
            self.cdp_port = kwargs["cdp_port"]
            self.cdp_base = f"http://localhost:{self.cdp_port}"
        if "auto_restart" in kwargs:
            self.auto_restart = kwargs["auto_restart"]

    def _is_running(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.cdp_base}/json/version", timeout=2).read()
            return True
        except Exception:
            return False

    def _get_ws_url(self) -> str:
        data = json.loads(urllib.request.urlopen(f"{self.cdp_base}/json/list").read())
        pages = [t for t in data if t["type"] == "page"]
        if not pages:
            raise RuntimeError("No page target in CDP")
        return pages[0]["webSocketDebuggerUrl"]

    async def count_threads(self, since: int | None = None) -> int:
        """Return approximate count of available sessions."""
        if not self._is_running():
            if self.auto_restart:
                if not _restart_windsurf_with_cdp(self.cdp_port):
                    return 0
            else:
                return 0

        try:
            ws_url = self._get_ws_url()
            cdp = CDPClient(ws_url, timeout=5)
            try:
                cdp.call("Runtime.enable")
                sessions_raw = cdp.eval("""
                (function() {
                    let raw = localStorage.getItem('cascade-open-sessions-by-workspace');
                    return raw || '{}';
                })()
                """)
                sessions_by_ws = json.loads(sessions_raw or "{}")
                count = 0
                for ws_data in sessions_by_ws.values():
                    for tab in ws_data.get("tabs", []):
                        if tab.get("type") == "cascade":
                            count += 1
                return count
            finally:
                cdp.close()
        except Exception:
            return 0

    async def threads(self, since: int | None = None) -> AsyncIterator[IngestedThread]:
        if websocket is None:
            raise RuntimeError(
                "websocket-client required. Install with: uv add websocket-client"
            )

        if not self._is_running():
            if self.auto_restart:
                if not _restart_windsurf_with_cdp(self.cdp_port):
                    raise RuntimeError(
                        "Failed to auto-restart Windsurf with CDP. "
                        "Please start manually: windsurf --remote-debugging-port=9222"
                    )
            else:
                raise RuntimeError(
                    "Windsurf not running with CDP. "
                    "Start with: windsurf --remote-debugging-port=9222 "
                    "Or use init(auto_restart=True) to auto-restart."
                )

        ws_url = self._get_ws_url()
        cdp = CDPClient(ws_url)

        try:
            cdp.call("Runtime.enable")

            # Get list of sessions from localStorage
            sessions_raw = cdp.eval("""
            (function() {
                let raw = localStorage.getItem('cascade-open-sessions-by-workspace');
                return raw || '{}';
            })()
            """)

            sessions_by_ws = json.loads(sessions_raw or "{}")
            session_ids: list[str] = []

            for ws_key, ws_data in sessions_by_ws.items():
                for tab in ws_data.get("tabs", []):
                    if tab.get("type") == "cascade":
                        session_ids.append(tab["id"])

            if not session_ids:
                # Try to get current active session only
                count = _store_trajectory(cdp)
                if count > 0:
                    conv = _extract_conversation(cdp)
                    if conv:
                        thread = _convert_to_thread(conv)
                        if since and thread.updated_at and thread.updated_at < since:
                            pass
                        else:
                            yield thread
                return

            for session_id in session_ids:
                # Click session to activate it
                cdp.eval(f"""
                (function() {{
                    let el = document.getElementById('windsurf.cascadePanel');
                    if (!el) return;
                    let items = el.querySelectorAll('[data-session-id]');
                    for (let item of items) {{
                        if (item.dataset.sessionId === {json.dumps(session_id)}) {{
                            item.click();
                            return true;
                        }}
                    }}
                    return false;
                }})()
                """)
                time.sleep(1)

                count = _store_trajectory(cdp)
                if count <= 0:
                    continue

                conv = _extract_conversation(cdp)
                if not conv:
                    continue

                thread = _convert_to_thread(conv)
                if since and thread.updated_at and thread.updated_at < since:
                    continue

                yield thread

        finally:
            cdp.close()
