#!/usr/bin/env python3
"""
Intercept language server requests for 30 seconds and save to file.
Analyze subdomain patterns.
"""
import asyncio
import json
import urllib.request
import websockets
from datetime import datetime
from pathlib import Path

async def intercept_requests(cdp_port=9222, duration=30):
    """Intercept language server requests for given duration."""
    cdp_url = f"http://127.0.0.1:{cdp_port}/json"
    print(f"Connecting to CDP on port {cdp_port}...")
    
    requests_data = []
    
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
                return requests_data
            
            ws_url = page_target['webSocketDebuggerUrl']
            print(f"Connecting to WebSocket: {ws_url}")
    except Exception as e:
        print(f"Error getting CDP targets: {e}")
        return requests_data
    
    async with websockets.connect(ws_url) as ws:
        # Enable Network domain
        await ws.send(json.dumps({
            "id": 1,
            "method": "Network.enable",
            "params": {}
        }))
        
        print(f"Listening for language server requests for {duration}s...")
        start_time = asyncio.get_event_loop().time()
        
        while asyncio.get_event_loop().time() - start_time < duration:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                
                if data.get('method') == 'Network.requestWillBeSent':
                    request_data = data['params']['request']
                    url = request_data['url']
                    headers = request_data.get('headers', {})
                    
                    if 'localhost' in url and 'language_server' in url.lower():
                        # Extract subdomain
                        from urllib.parse import urlparse
                        parsed = urlparse(url)
                        hostname = parsed.hostname
                        subdomain = hostname.split('.')[0] if hostname else 'unknown'
                        
                        # Extract endpoint name
                        path_parts = parsed.path.split('/')
                        endpoint = path_parts[-1] if path_parts else 'unknown'
                        
                        # Extract CSRF token if present
                        csrf_token = None
                        for key, value in headers.items():
                            if 'csrf' in key.lower():
                                csrf_token = value
                        
                        request_info = {
                            'timestamp': datetime.now().isoformat(),
                            'url': url,
                            'hostname': hostname,
                            'subdomain': subdomain,
                            'endpoint': endpoint,
                            'csrf_token': csrf_token,
                            'method': request_data.get('method'),
                        }
                        
                        requests_data.append(request_info)
                        print(f"  [{len(requests_data)}] {subdomain}.localhost - {endpoint}")
            
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error processing message: {e}")
        
        print(f"Captured {len(requests_data)} requests")
        return requests_data

if __name__ == "__main__":
    print("Intercepting language server requests for 30 seconds...\n")
    requests = asyncio.run(intercept_requests(duration=30))
    
    # Save to file
    output_file = Path("/tmp/ls_requests.json")
    output_file.write_text(json.dumps(requests, indent=2))
    print(f"\nSaved to {output_file}")
    
    # Analyze subdomain patterns
    if requests:
        print("\n=== Subdomain Analysis ===")
        
        # Get all subdomains
        subdomains = [r['subdomain'] for r in requests]
        unique_subdomains = sorted(set(subdomains))
        print(f"Unique subdomains: {unique_subdomains}")
        print(f"Total requests: {len(requests)}")
        
        # Check if subdomains are consecutive (alphabetically)
        if len(unique_subdomains) > 1:
            is_consecutive = True
            for i in range(len(unique_subdomains) - 1):
                if ord(unique_subdomains[i+1]) != ord(unique_subdomains[i]) + 1:
                    is_consecutive = False
                    break
            print(f"Subdomains are consecutive: {is_consecutive}")
        
        # Check if subdomains are related to endpoints
        endpoint_subdomain_map = {}
        for r in requests:
            endpoint = r['endpoint']
            subdomain = r['subdomain']
            if endpoint not in endpoint_subdomain_map:
                endpoint_subdomain_map[endpoint] = set()
            endpoint_subdomain_map[endpoint].add(subdomain)
        
        print("\nEndpoint to subdomain mapping:")
        for endpoint, subdomains in endpoint_subdomain_map.items():
            print(f"  {endpoint}: {sorted(subdomains)}")
        
        # Check if subdomain changes over time
        print("\nSubdomain sequence over time:")
        for i, r in enumerate(requests):
            print(f"  {i+1}. {r['timestamp']} - {r['subdomain']}.localhost - {r['endpoint']}")
    else:
        print("No requests captured")
