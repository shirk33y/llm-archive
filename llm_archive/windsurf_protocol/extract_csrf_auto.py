#!/usr/bin/env python3
"""
Extract CSRF token via CDP without user interaction by searching window object.
"""
import asyncio
import json
import urllib.request
import tempfile
from pathlib import Path
import websockets

async def extract_csrf_auto(cdp_port=9222):
    """Extract CSRF token by searching window object via CDP."""
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
        # Enable Runtime domain
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.enable",
            "params": {}
        }))
        
        # Search for CSRF token in window object - more thorough search
        search_script = """
        (function() {
            const results = [];
            
            // Helper to recursively search object
            function searchObject(obj, path, depth) {
                if (depth > 15) return; // Limit depth
                
                try {
                    // Check if this is a string that matches UUID pattern
                    if (typeof obj === 'string' && obj.match(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)) {
                        results.push({path: path, value: obj});
                    }
                    
                    // Search object properties
                    if (obj && typeof obj === 'object') {
                        for (let key in obj) {
                            if (obj.hasOwnProperty(key)) {
                                try {
                                    const newPath = path ? path + '.' + key : key;
                                    
                                    // Check property name for csrf/token
                                    if (key.toLowerCase().includes('csrf') || key.toLowerCase().includes('token')) {
                                        if (typeof obj[key] === 'string' && obj[key].match(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)) {
                                            results.push({path: newPath, value: obj[key]});
                                        }
                                    }
                                    
                                    searchObject(obj[key], newPath, depth + 1);
                                } catch (e) {
                                    // Ignore errors accessing properties
                                }
                            }
                        }
                    }
                } catch (e) {
                    // Ignore errors
                }
            }
            
            // Search window object
            searchObject(window, 'window', 0);
            
            return results;
        })()
        """
        
        await ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {
                "expression": search_script,
                "awaitPromise": True
            }
        }))
        
        msg = await ws.recv()
        data = json.loads(msg)
        
        if data.get('result') and data['result'].get('result'):
            results = data['result']['result']['value']
            print(f"Found {len(results)} potential CSRF tokens")
            
            # Filter for most likely candidates
            for result in results:
                path = result.get('path', '')
                if 'csrf' in path.lower() or 'token' in path.lower():
                    print(f"  Likely candidate: {path} = {result['value']}")
                    return result['value']
            
            # If no obvious candidates, return the first UUID found
            if results:
                print(f"  Using first UUID found: {results[0]['path']} = {results[0]['value']}")
                return results[0]['value']
        
        print("No CSRF token found")
        return None

if __name__ == "__main__":
    print("Extracting CSRF token from Windsurf via CDP (auto)\n")
    token = asyncio.run(extract_csrf_auto())
    if token:
        print(f"\nCSRF Token: {token}")
    else:
        print("\nFailed to extract CSRF token")
