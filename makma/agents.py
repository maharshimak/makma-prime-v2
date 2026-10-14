"""Routed specialist profiles, not fictional background agent processes."""
from __future__ import annotations

AGENTS = [
    {"id": "core", "name": "Core", "role": "General planning and orchestration", "status": "available", "tools": []},
    {"id": "research", "name": "Research", "role": "Source-backed research when a model is configured", "status": "model_required", "tools": ["web_search", "read_text_file", "recall", "create_note", "create_mission"]},
    {"id": "dev", "name": "Developer", "role": "Inspect project files and explain changes", "status": "available", "tools": ["list_files", "search_files", "read_text_file", "create_note", "create_mission", "recall"]},
    {"id": "career", "name": "Career", "role": "Plan applications and research opportunities", "status": "model_required", "tools": ["web_search", "recall", "create_note", "create_mission", "list_missions"]},
    {"id": "memory", "name": "Memory", "role": "Save and retrieve local knowledge", "status": "available", "tools": ["remember", "recall", "list_missions"]},
    {"id": "control", "name": "Control", "role": "Inspect the computer and request guarded actions", "status": "available", "tools": ["get_system_status", "top_processes", "open_app", "open_url", "list_files", "search_files", "read_text_file", "create_note", "create_folder", "type_text", "press_hotkey", "mouse_click"]},
    {"id": "vision", "name": "Vision", "role": "Analyze an explicitly captured image", "status": "model_required", "tools": []},
    {"id": "guard", "name": "Guard", "role": "Deterministic policy enforced on every tool action", "status": "policy", "tools": []},
]


def route_agent(text: str) -> dict:
    import re
    rules = [
        ("memory", r"\b(remember|recall|memory)\b"),
        ("control", r"\b(computer|cpu|ram|lag|processes|notepad|calculator)\b"),
        ("career", r"\b(career|recruiter|resume|interview|job application)\b"),
        ("dev", r"\b(debug|code|python|repository|refactor|software)\b"),
        ("research", r"\b(research|latest|news|sources)\b"),
    ]
    agent_id = next((key for key, pattern in rules if re.search(pattern, text.lower())), "core")
    return next(dict(agent) for agent in AGENTS if agent["id"] == agent_id)
