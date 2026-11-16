"""Explicit schemas for the public Agent tool interface."""

from copy import deepcopy

ENV = {"type": "string", "minLength": 1, "description": "Configured environment name"}
TASK = {"type": "string", "minLength": 1, "description": "Owned task UUID from codex_submit"}
TEXT = {"type": "string", "minLength": 1, "description": "Objective or additional input"}


def tool_definitions():
    definitions = [
        ("environments", "List authorized environments and workspace aliases.", {}, []),
        (
            "submit",
            "Submit a task; resume requires an owned thread_id. Read results with codex_task.",
            {
                "env": ENV,
                "workspace": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Configured workspace alias",
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
            "Read an owned task, native evidence and progress; optionally wait up to 30 seconds.",
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
            "Request cancellation of an owned task; await native confirmation.",
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
