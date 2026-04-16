#!/usr/bin/env python3
"""
Extract CSRF token via CDP without user interaction - safe version.
Waits for automatic language server requests instead of deep object traversal.
"""
import asyncio
import json
import urllib.request
import websockets

async def extract_csrf_safe(cdp_port=9222, timeout=30):
    """Extract CSRF token by waiting for automatic language server requests."""
    cdp_url = f"http://127.0.0.1:{cdp_port}/json"
    print(f"Connecting to CDP on port {cdp_port}...")
    
    try:
        with urllib.request.urlopen(cdp_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
            print(f"Found {len(targets)} CDP targets")
            
            page_target = None
            for target in targets:
                if target.get('type') == 'page':
                    page_target = target
                    break
            
            if not page_target:
                print("No page target found")
                return None
            
            ws_url = page_target['webSocketDebuggerUrl']
            print(f"Connecting to WebSocket: {ws_url}")
    except Exception as e:
        print(f"Error getting CDP targets: {e}")
        return None
    
    async with websockets.connect(ws_url) as ws:
        # Enable Network domain
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.enable",
            "params": {}
        }))
        
        print(f"Listening for automatic language server requests (timeout: {timeout}s)...")
        
        csrf_token = None
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
                        print(f"  Intercepted request: {url}")
                        for key, value in headers.items():
                            if 'csrf' in key.lower():
                                csrf_token = value
                                print(f"  Found CSRF token: {csrf_token}")
                                return csrf_token
            
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error processing message: {e}")
        
        print("No language server request detected within timeout")
        print("Try loading a trajectory in Windsurf to trigger a request")
        return None

if __name__ == "__main__":
    print("Extracting CSRF token from Windsurf via CDP (safe mode)\n")
    token = asyncio.run(extract_csrf_safe())
    if token:
        print(f"\nCSRF Token: {token}")
    else:
        print("\nFailed to extract CSRF token")
