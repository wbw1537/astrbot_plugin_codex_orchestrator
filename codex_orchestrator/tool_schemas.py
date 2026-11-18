"""Explicit schemas for the public Agent tool interface."""

from copy import deepcopy

ENV = {
    "type": "string",
    "minLength": 1,
    "description": "Configured Codex App Server environment name",
}
TASK = {"type": "string", "minLength": 1, "description": "Owned task UUID from codex_submit"}
TEXT = {"type": "string", "minLength": 1, "description": "Objective or additional input"}


def tool_definitions():
    definitions = [
        (
            "environments",
            "List authorized Codex App Server environments, connection status and workspace "
            "aliases. AstrBot dispatches tasks. Codex executes them in the selected project's "
            "working directory. With execution.transport=websocket, AstrBot connects to a "
            "separately running App Server, which can be on another host. With stdio it "
            "launches a subprocess. execution.configuredHost, when present, "
            "is the admin's label for the Codex host.",
            {},
            [],
        ),
        (
            "submit",
            "Dispatch a task from AstrBot to the selected Codex App Server environment. "
            "The workspace selects its project working directory. Resume requires an owned "
            "thread_id. Use codex_task to read results and relay pending interactions.",
            {
                "env": ENV,
                "workspace": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Alias for a project working directory on the Codex side",
                },
                "text": TEXT,
                "mode": {"type": "string", "enum": ["new", "resume"], "default": "new"},
                "thread_id": {
                    "type": "string",
                    "default": "",
                    "description": "Owned native thread ID, required for resume",
                },
            },
            ["env", "workspace", "text"],
        ),
        (
            "task",
            "Read an owned Codex task, native evidence and progress. Attribute command output "
            "to the Codex task environment. Use separate diagnostic evidence for the server "
            "host, container placement and service health. Optionally wait up to 30 seconds "
            "for completion or a pending interaction. For approvals, describe the request and "
            "show the exact decisionCommands for the original user to send in the same "
            "AstrBot conversation. The user can approve with /codex approve ENV TASK_ID "
            "INTERACTION_ID or deny with /codex deny ENV TASK_ID INTERACTION_ID when offered. "
            "For questions and forms, explain the requested input and relay the /codex answer "
            "command and JSON format, or the decline command when offered. Keep the task active "
            "while the user decides. Tell the user how to check status after responding.",
            {
                "env": ENV,
                "task_id": TASK,
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30, "default": 0},
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "interaction_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Pending-interaction pagination offset",
                },
            },
            ["env", "task_id"],
        ),
        ("sessions", "List this actor's owned Codex sessions.", {"env": ENV}, ["env"]),
        (
            "cancel",
            "Request cancellation of an owned task. If it returns cancelling, use codex_task "
            "to check status and confirm cancellation once it returns interrupted. For tasks "
            "waiting for user interaction, relay the request and keep the task active unless "
            "the user asks to cancel it.",
            {"env": ENV, "task_id": TASK},
            ["env", "task_id"],
        ),
        (
            "steer",
            "Add input to the current turn of an owned task.",
            {"env": ENV, "task_id": TASK, "text": TEXT},
            ["env", "task_id", "text"],
        ),
    ]
    return [
        {
            "name": "codex_" + name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": deepcopy(properties),
                "required": required,
                "additionalProperties": False,
            },
            "callback": "llm_" + name,
        }
        for name, description, properties, required in definitions
    ]
