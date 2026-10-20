"""
Send one A2A message to a running agent and print the task state and reply.

Usage (from the repo root, with the virtualenv active):
    python tests/send_a2a_message.py "Recommend 3 sci-fi movies"
    python tests/send_a2a_message.py "What genres do you like?" --url http://localhost:10006/

The request is a raw JSON-RPC 2.0 "SendMessage" call (a2a-sdk 1.x format),
sent with httpx and no A2A client library. Prints "State: <task state>",
then the text of every artifact part, then any status message text (for
TASK_STATE_FAILED this is the underlying error, e.g. a Gemini quota error).
Exit code 0 only for TASK_STATE_COMPLETED.
"""
import argparse
import json
import sys
import uuid

import httpx


def main() -> int:
    """Build the JSON-RPC request, POST it, and print the resulting task."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("text", help="Message to send")
    parser.add_argument("--url", default="http://localhost:10005/", help="Agent URL (default: Movie Agent)")
    args = parser.parse_args()

    # a2a-sdk 1.x JSON-RPC: method "SendMessage", protobuf-style enum values for roles.
    # Field names are the protobuf JSON (camelCase) names. No contextId/taskId
    # is sent, so the server creates a new task and context for this message.
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": str(uuid.uuid4()),
                "parts": [{"text": args.text}],
            }
        },
    }
    # A2A-Version declares the protocol version (a2a-sdk's ClientFactory sends the same header).
    # The call blocks until the agent finishes (LLM + tool calls), hence the long timeout.
    response = httpx.post(args.url, json=payload, headers={"A2A-Version": "1.0"}, timeout=180)
    response.raise_for_status()
    body = response.json()
    # Protocol-level failures (unknown method, bad params, ...) come back as a JSON-RPC error object.
    if "error" in body:
        print("JSON-RPC error:", json.dumps(body["error"], indent=2))
        return 1

    # A successful SendMessage returns {"result": {"task": {...}}}.
    task = body["result"]["task"]
    state = task["status"]["state"]
    print("State:", state)
    # The agent's answer is stored as artifact parts.
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if "text" in part:
                print(part["text"])
    # The final status message carries the error text when the task failed.
    for part in task["status"].get("message", {}).get("parts", []):
        if "text" in part:
            print(part["text"])
    return 0 if state == "TASK_STATE_COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
