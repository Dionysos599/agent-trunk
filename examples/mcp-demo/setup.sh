#!/bin/sh
# Set up the demo: put the project in the store, put the ten tasks on a board, and create one folder per agent
# with the MCP configuration for that agent.
#
#   ./setup.sh [NAME] [TASKS_FILE]       NAME is used for the project and the board (default: demo)
#
# Needs the gateway running (make run-gateway ...). AGENTTRUNK_URL says where it is.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
NAME="${1:-demo}"
TASKS="${2:-$HERE/tasks.json}"
URL="${AGENTTRUNK_URL:-http://127.0.0.1:8080}"
AT="$ROOT/bin/agenttrunk"

if ! curl -s -m 3 "$URL/health" >/dev/null 2>&1; then
  echo "Cannot reach the gateway at $URL. Start it first (from the repository root):" >&2
  echo "  make run-gateway ARGS=\"--port 8080 --groups 2 --replicas 3\"" >&2
  echo "or set AGENTTRUNK_URL to where it runs." >&2
  exit 1
fi

"$AT" --url "$URL" --main "$NAME" init --from "$HERE/project"
"$AT" --url "$URL" --main "$NAME" board "$NAME" --tasks "$TASKS"

for AGENT in alice bob carol; do
  DIR="$HERE/agents/$NAME/$AGENT"
  mkdir -p "$DIR"
  cat > "$DIR/.mcp.json" <<JSON
{
  "mcpServers": {
    "agenttrunk": {
      "command": "$AT",
      "args": ["--url", "$URL", "--main", "$NAME", "mcp", "--board", "$NAME", "--agent", "$AGENT",
               "--workdir", "./work", "--validate-cmd", "python3 -m unittest discover"]
    }
  }
}
JSON
done

echo
echo "Ready. Project and board are called '$NAME'. Open one terminal per agent:"
for AGENT in alice bob carol; do echo "  cd $HERE/agents/$NAME/$AGENT && claude"; done
echo
echo "Watch the board:  $AT --url $URL --main $NAME board $NAME"
echo "Check the result: $AT --url $URL --main $NAME export /tmp/$NAME-result && python3 $HERE/check.py /tmp/$NAME-result"
