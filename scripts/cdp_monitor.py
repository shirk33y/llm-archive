#!/usr/bin/env python3
"""CDP monitor that also launches Chrome and captures network traffic."""

import asyncio
import json
import os
import subprocess
import sys
import time
import websockets

# Chrome profile to reuse
CHROME_PROFILE = os.path.expanduser("~/.config/google-chrome")
CHROME_PORT = 9222


def launch_chrome():
    """Launch Chrome with CDP if not already running."""
    import urllib.request

    try:
        targets = json.loads(
            urllib.request.urlopen(f"http://localhost:{CHROME_PORT}/json", timeout=1).read()
        )
        if targets:
            print("Chrome already running with CDP")
            return True
    except Exception:
        pass

    print("Launching Chrome...")
    subprocess.Popen(
        [
            "flatpak",
            "run",
            "com.google.Chrome",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={CHROME_PROFILE}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to start
    for _ in range(30):
        time.sleep(1)
        try:
            targets = json.loads(
                urllib.request.urlopen(f"http://localhost:{CHROME_PORT}/json", timeout=1).read()
            )
            if targets:
                print("Chrome launched")
                return True
        except Exception:
            continue
    return False


def find_chatgpt_page(targets):
    """Find chatgpt page from targets."""
    for t in targets:
        if t.get("type") == "page":
            url = t.get("url", "")
            title = t.get("title", "")
            if "chatgpt" in url.lower() or "chatgpt" in title.lower() or "openai" in url.lower():
                return t
    # Return first page if no chatgpt
    for t in targets:
        if t.get("type") == "page":
            return t
    return None


async def monitor():
    """Monitor CDP traffic."""
    import urllib.request

    targets = json.loads(
        urllib.request.urlopen(f"http://localhost:{CHROME_PORT}/json", timeout=5).read()
    )
    page = find_chatgpt_page(targets)

    if not page:
        print("No page found")
        return

    ws_url = page["webSocketDebuggerUrl"]
    title = page.get("title", "?")[:60]
    page_url = page.get("url", "?")[:80]
    print(f"\n{'=' * 80}")
    print(f"Monitoring: {title}")
    print(f"URL: {page_url}")
    print("=" * 80)

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))

        seen_urls = set()
        api_requests = []

        for _ in range(600):  # 60 seconds
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
                data = json.loads(msg)

                method = data.get("method", "")
                params = data.get("params", {})

                if method == "Network.requestWillBeSent":
                    url = params.get("request", {}).get("url", "")
                    method_name = params.get("request", {}).get("method", "GET")

                    # Skip noise
                    if any(x in url for x in ["chrome-extension", "chrome://", "devtools", ".js?"]):
                        continue
                    if "accounts.google.com" in url or "googleapis.com/oauth" in url:
                        continue

                    if url not in seen_urls:
                        seen_urls.add(url)

                        # Check if API call
                        is_api = any(
                            x in url for x in ["/api/", "/v1/", "chatgpt.com", "openai.com"]
                        )
                        prefix = "[API] " if is_api else "      "

                        print(f"{prefix}{method_name} {url[:100]}")

                        if is_api:
                            api_requests.append(url)

                            # Print relevant headers
                            headers = params.get("request", {}).get("headers", {})
                            for k, v in headers.items():
                                if k.lower() in [
                                    "authorization",
                                    "cookie",
                                    "content-type",
                                    "openai-organization",
                                ]:
                                    if "authorization" in k.lower():
                                        print(f"         {k}: Bearer [TOKEN HIDDEN]")
                                    elif "cookie" in k.lower():
                                        print(f"         {k}: [COOKIES HIDDEN]")
                                    else:
                                        print(f"         {k}: {v}")

                elif method == "Network.responseReceived":
                    url = params.get("response", {}).get("url", "")
                    if "accounts.google.com" in url:
                        continue
                    status = params.get("response", {}).get("status", 0)
                    if url in seen_urls and status >= 200:
                        ct = params.get("response", {}).get("mimeType", "")
                        if "json" in ct:
                            print(f"      --> [{status}] JSON ({ct})")

            except asyncio.TimeoutError:
                continue

        print(f"\n\n{'=' * 80}")
        print(f"SUMMARY: {len(api_requests)} API endpoints found")
        print("=" * 80)

        with open("cdp_api_endpoints.txt", "w") as f:
            for url in api_requests:
                f.write(url + "\n")
        print("API endpoints saved to cdp_api_endpoints.txt")


def main():
    if "--no-launch" not in sys.argv:
        launch_chrome()

    print("\nWaiting for you to open chatgpt.com and interact...")
    print("Press Enter to start monitoring (or wait 10 seconds)")

    try:
        for i in range(10, 0, -1):
            print(f"\rStarting in {i}s... (Ctrl+C to start now)", end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 60 + "\rStarting now...")
    except KeyboardInterrupt:
        print("\nStarting now...")

    monitor()


if __name__ == "__main__":
    main()
