#!/usr/bin/env python3
"""
Extract CSRF token by intercepting actual request via CDP.
"""
import asyncio
import json
import urllib.request
import tempfile
from pathlib import Path
import websockets

async def extract_csrf_from_request(cdp_port=9222, duration=60):
    """Extract CSRF token by intercepting requests."""
    output_dir = Path(tempfile.gettempdir()) / "windsurf_csrf"
    output_dir.mkdir(exist_ok=True)
    print(f"Output directory: {output_dir}")
    
    # Connect to CDP using HTTP to get targets
    cdp_url = f"http://127.0.0.1:{cdp_port}/json"
    print(f"Connecting to CDP on port {cdp_port}...")
    
    try:
        with urllib.request.urlopen(cdp_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
            print(f"Found {len(targets)} CDP targets")
            
            # Find the page target
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
        
        print(f"Listening for requests for {duration} seconds...")
        print("Please load a trajectory in Windsurf to trigger a request...")
        
        request_count = 0
        start_time = asyncio.get_event_loop().time()
        csrf_token = None
        
        while asyncio.get_event_loop().time() - start_time < duration:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                
                if data.get('method') == 'Network.requestWillBeSent':
                    request_data = data['params']['request']
                    url = request_data['url']
                    headers = request_data.get('headers', {})
                    
                    if 'language_server' in url.lower() or 'GetCascadeTrajectory' in url:
                        request_count += 1
                        print(f"\nIntercepted request {request_count}: {url}")
                        print(f"Headers: {list(headers.keys())}")
                        
                        # Look for CSRF token in headers
                        for key, value in headers.items():
                            if 'csrf' in key.lower():
                                print(f"  Found CSRF header: {key} = {value}")
                                csrf_token = value
                                (output_dir / "csrf_token.txt").write_text(value)
                                return csrf_token
                        
                        # Save all headers for analysis
                        headers_file = output_dir / f"{request_count:03d}_headers.json"
                        headers_file.write_text(json.dumps(headers, indent=2))
                
                elif data.get('method') == 'Network.responseReceived':
                    response = data['params']['response']
                    url = response['url']
                    
                    if 'language_server' in url.lower() or 'GetCascadeTrajectory' in url:
                        print(f"Response received: {response['status']}")
            
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error processing message: {e}")
        
        print(f"\nCaptured {request_count} requests")
        print(f"Output directory: {output_dir}")
        
        if csrf_token:
            print(f"CSRF Token: {csrf_token}")
        else:
            print("No CSRF token found in intercepted requests")
        
        return csrf_token

if __name__ == "__main__":
    print("Extracting CSRF token from Windsurf requests via CDP\n")
    token = asyncio.run(extract_csrf_from_request())
    if token:
        print(f"\nCSRF Token: {token}")
    else:
        print("\nFailed to extract CSRF token")
