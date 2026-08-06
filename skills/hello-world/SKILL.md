# Hello World Skill

A minimal example skill demonstrating the Karakos skill system.

## What It Does

Provides a `hello_world` tool that returns a greeting message. Use this as a template for building your own skills.

## Files

```
hello-world/
├── SKILL.md        # This file — skill documentation
├── tools.json      # Tool definitions (MCP schema)
├── scripts/
│   └── hello_world.py  # Tool implementation
└── assets/         # Static assets (unused in this example)
```

## Usage

Once installed in `skills/hello-world/`, the tool is automatically discovered by the MCP tool server and available to all agents:

```
Tool: hello_world
Input: {"name": "World"}
Output: {"greeting": "Hello, World!", "timestamp": "2026-03-30T12:00:00Z"}
```
