#!/usr/bin/env python3
"""
Hello World skill implementation.

The MCP tool server calls this script with TOOL_ARGS as a JSON environment variable.
Output JSON to stdout — it's returned as the tool result.
"""

import json
import os
from datetime import datetime, timezone


def main():
    args = json.loads(os.environ.get("TOOL_ARGS", "{}"))
    name = args.get("name", "World")

    result = {
        "greeting": f"Hello, {name}!",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "skill": "hello-world",
        "version": "1.0.0",
    }

    print(json.dumps(result))


if __name__ == "__main__":
    main()
