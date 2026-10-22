#!/usr/bin/env bash
# Call one MCP tool over HTTP and print a short result. Usage: mcp_call.sh <tool> '<json args>' [chars]
# Env: MCP_URL (default http://localhost:8001/mcp), MCP_TOKEN
curl -s -X POST "${MCP_URL:-http://localhost:8001/mcp}" \
  -H "Authorization: Bearer ${MCP_TOKEN}" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: 2025-06-18" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":${2:-{\}}}}" |
python3 -c "
import sys, json
d = json.load(sys.stdin); r = d.get('result', d)
t = r.get('content', [{}])[0].get('text', '') if isinstance(r, dict) else str(r)
print('ERR' if r.get('isError') else 'OK', t[:int(sys.argv[1])].replace(chr(10), ' '))" "${3:-300}"
