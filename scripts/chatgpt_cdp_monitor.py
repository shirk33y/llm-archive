#!/usr/bin/env python3
"""Start Chrome with CDP, wait for user to login/interact, and intercept API requests."""

import asyncio
import json
import os
import subprocess
import signal
import sys
import time
from pathlib import Path
import websockets

# Use port 9333 to avoid conflict with Windsurf on 9222
CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
CHATGPT_URL = "https://chatgpt.com"


def find_chrome_binary():
    """Find Chrome or Chromium binary."""
    import shutil

    candidates = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if shutil.which(c):
            return c

    # Try flatpak
    result = subprocess.run(["flatpak", "info", "com.google.Chrome"], capture_output=True)
    if result.returncode == 0:
        return ["flatpak", "run", "com.google.Chrome"]

    result = subprocess.run(["flatpak", "info", "org.chromium.Chromium"], capture_output=True)
    if result.returncode == 0:
        return ["flatpak", "run", "org.chromium.Chromium"]

    raise RuntimeError("Could not find Chrome or Chromium")


def find_chrome_profile():
    """Find existing Chrome profile."""
    candidates = [
        Path.home() / ".config/google-chrome",
        Path.home() / ".config/chromium",
        Path.home() / ".var/app/com.google.Chrome/config/google-chrome",
        Path.home() / ".var/app/org.chromium.Chromium/config/chromium",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


async def monitor_cdp(ws_url: str, auth_timeout: int, interact_timeout: int):
    """Monitor CDP and intercept API requests."""
    seen_urls = set()
    auth_urls = []

    async with websockets.connect(ws_url) as ws:
        # Enable Network monitoring
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))

        start = time.time()
        logged_in = False

        while True:
            elapsed = time.time() - start
            remaining = auth_timeout - elapsed

            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)

                method = data.get("method", "")
                params = data.get("params", {})

                if method == "Network.requestWillBeSent":
                    url = params.get("request", {}).get("url", "")
                    req_method = params.get("request", {}).get("method", "GET")

                    # Skip noise
                    if any(
                        x in url
                        for x in [
                            "chrome-extension",
                            "chrome://",
                            ".js?",
                            "_next/",
                            "fonts.",
                            "googleapis.com/css",
                        ]
                    ):
                        continue

                    # Track ChatGPT API calls
                    is_api = "chatgpt.com" in url and ("/backend-api/" in url or "/api/" in url)

                    if url not in seen_urls:
                        seen_urls.add(url)
                        marker = "*** " if is_api else "    "
                        print(f"{marker}[{elapsed:.0f}s] {req_method} {url[:100]}")

                        if is_api:
                            auth_urls.append((elapsed, req_method, url))
                            logged_in = True

                        # Print auth headers for API calls
                        if is_api and elapsed < 5:
                            headers = params.get("request", {}).get("headers", {})
                            for k, v in headers.items():
                                if k.lower() in ["authorization", "cookie"]:
                                    print(
                                        f"       {k}: {'[TOKEN]' if 'authorization' in k.lower() else '[COOKIES]'}"
                                    )

                elif method == "Network.responseReceived":
                    url = params.get("response", {}).get("url", "")
                    if url in seen_urls:
                        status = params.get("response", {}).get("status", 0)
                        ct = params.get("response", {}).get("mimeType", "")
                        if "json" in ct:
                            print(f"    --> [{status}] JSON ({ct[:30]})")

            except asyncio.TimeoutError:
                # Check timeouts
                if elapsed < auth_timeout:
                    if not logged_in and elapsed > 5 and int(elapsed) % 10 == 0:
                        print(f"\n[Waiting for login... {int(remaining)}s remaining]\n")
                else:
                    # Auth timeout passed, now interaction timeout
                    interact_elapsed = elapsed - auth_timeout
                    interact_remaining = interact_timeout - interact_elapsed
                    if int(interact_elapsed) % 10 == 0:
                        print(
                            f"\n[Interaction period: {int(interact_elapsed)}s / {interact_timeout}s]\n"
                        )

                    if interact_elapsed >= interact_timeout:
                        break

        print(f"\n{'=' * 60}")
        print(f"Captured {len(auth_urls)} API requests")
        print(f"{'=' * 60}")

        # Save API endpoints to file
        with open("chatgpt_api_endpoints.txt", "w") as f:
            for elapsed, method, url in auth_urls:
                f.write(f"{method} {url}\n")
        print("API endpoints saved to chatgpt_api_endpoints.txt")


def main():
    print(f"Starting Chrome with CDP on port {CDP_PORT}...")
    print(f"Will navigate to {CHATGPT_URL}")
    print(f"Waiting {60}s for login, then {30}s for interaction")
    print()

    chrome = find_chrome_binary()
    profile = find_chrome_profile()

    if isinstance(chrome, list):
        cmd = chrome + [
            "--remote-debugging-port=" + str(CDP_PORT),
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            CHATGPT_URL,
        ]
        if profile:
            cmd.insert(-1, f"--user-data-dir={profile}")
    else:
        cmd = [
            chrome,
            "--remote-debugging-port=" + str(CDP_PORT),
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            CHATGPT_URL,
        ]
        if profile:
            cmd.extend([f"--user-data-dir={profile}"])

    print(f"Chrome command: {' '.join(cmd[:5])}...")
    if profile:
        print(f"Using profile: {profile}")
    print()

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for Chrome to start
    print("Waiting for Chrome to start...")
    for i in range(30):
        time.sleep(1)
        try:
            import urllib.request

            resp = urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=1)
            targets = json.loads(resp.read())
            if targets:
                print(f"Chrome ready! (took {i + 1}s)")
                break
        except:
            continue
    else:
        print("Chrome failed to start!")
        proc.terminate()
        return 1

    # Get WebSocket URL
    ws_url = None
    for t in targets:
        if t.get("type") == "page":
            ws_url = t["webSocketDebuggerUrl"]
            break

    if not ws_url:
        print("No page found in Chrome")
        proc.terminate()
        return 1

    print(f"\nNavigate to ChatGPT and log in...")
    print(f"Monitor will start in 5 seconds...\n")
    time.sleep(5)

    # Setup signal handlers
    def cleanup(signum, frame):
        print("\n\nInterrupted. Saving data...")
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Run monitor
    try:
        asyncio.run(monitor_cdp(ws_url, auth_timeout=60, interact_timeout=30))
    finally:
        print("\nClosing Chrome...")
        proc.terminate()
        proc.wait()

    return 0


if __name__ == "__main__":
    sys.exit(main())
