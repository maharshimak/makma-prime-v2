import datetime as dt
import json
import platform
import subprocess
import webbrowser
import time
import uuid
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from pathlib import Path
from typing import Any, Dict

import psutil

from .memory import create_mission, list_missions, recall, remember, update_mission
from .policy import create_approval, risk_level, decision

HOME = Path.home()
SAFE_ROOTS = [Path(__file__).resolve().parents[1] / "workspace"]


def _safe_path(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    from .store import get_settings
    for root in map(Path, get_settings().get("allowed_directories", [str(p) for p in SAFE_ROOTS])):
        root = root.resolve()
        try:
            p.relative_to(root)
            return p
        except Exception:
            continue
    raise ValueError("Path is outside Mak'ma's configured directories.")


def get_system_status() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(HOME.anchor or "/"))
    try:
        battery = psutil.sensors_battery()
    except (NotImplementedError, FileNotFoundError, OSError):
        battery = None
    return {
        "ok": True,
        "os": platform.platform(),
        "hostname": platform.node(),
        "cpu_percent": psutil.cpu_percent(interval=0.15),
        "memory_percent": vm.percent,
        "memory_available_gb": round(vm.available / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "battery": None if battery is None else {"percent": battery.percent, "plugged": battery.power_plugged},
        "time": dt.datetime.now().astimezone().isoformat(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "network": psutil.net_io_counters()._asdict(),
    }


def top_processes(limit: int = 8) -> Dict[str, Any]:
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_percent"]):
        try:
            rows.append({
                "pid": proc.info["pid"],
                "name": proc.info["name"] or "unknown",
                "memory_percent": round(proc.info["memory_percent"] or 0, 2),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    rows.sort(key=lambda x: x["memory_percent"], reverse=True)
    return {"ok": True, "processes": rows[: max(1, min(int(limit), 25))]}


def open_url(url: str) -> Dict[str, Any]:
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return {"ok": False, "error": "Expected a public HTTP(S) URL without credentials."}
    ok = webbrowser.open(url)
    return {"ok": bool(ok), "url": url, "verification": "Browser launch requested; destination content not verified."}


def open_app(name: str) -> Dict[str, Any]:
    apps = {
        "notepad": ["notepad.exe"],
        "calculator": ["calc.exe"],
        "paint": ["mspaint.exe"],
        "explorer": ["explorer.exe"],
        "terminal": ["wt.exe"],
        "powershell": ["powershell.exe"],
        "settings": ["cmd.exe", "/c", "start", "ms-settings:"],
        "task manager": ["taskmgr.exe"],
    }
    key = name.strip().lower()
    if key not in apps:
        return {"ok": False, "error": f"App '{name}' is not in the allowlist.", "allowed": sorted(apps)}
    if platform.system() != "Windows":
        return {"ok": False, "error": "Application control requires Windows."}
    process = subprocess.Popen(apps[key], shell=False)
    return {"ok": True, "app": key, "pid": process.pid, "verification": "Process launched; window readiness not verified."}


def list_files(folder: str, limit: int = 50) -> Dict[str, Any]:
    p = _safe_path(folder)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "error": "Folder does not exist."}
    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:limit]:
        try:
            size = None if child.is_dir() else child.stat().st_size
        except OSError:
            size = None
        items.append({"name": child.name, "type": "folder" if child.is_dir() else "file", "path": str(child), "size": size})
    return {"ok": True, "folder": str(p), "items": items}


def search_files(query: str, folder: str = "Documents", limit: int = 30) -> Dict[str, Any]:
    roots = {"desktop": HOME / "Desktop", "documents": HOME / "Documents", "downloads": HOME / "Downloads"}
    root = roots.get(folder.lower(), Path(folder))
    root = _safe_path(str(root))
    q = query.lower().strip()
    matches = []
    started = time.monotonic()
    for index, child in enumerate(root.rglob("*")):
        if index > 20000 or time.monotonic() - started > 5:
            break
        try:
            _safe_path(str(child))
            if q in child.name.lower():
                matches.append({"name": child.name, "type": "folder" if child.is_dir() else "file", "path": str(child)})
                if len(matches) >= limit:
                    break
        except (PermissionError, OSError, ValueError):
            continue
    return {"ok": True, "query": query, "folder": str(root), "matches": matches}


def read_text_file(path: str, max_chars: int = 30000) -> Dict[str, Any]:
    p = _safe_path(path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "File does not exist."}
    allowed = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".yaml", ".yml", ".toml", ".ini", ".log"}
    if p.suffix.lower() not in allowed:
        return {"ok": False, "error": "This local reader only allows common text/code formats."}
    if p.name.lower().startswith(".env") or any(part.lower() in {".ssh", ".aws", ".azure", ".gnupg"} for part in p.parts):
        return {"ok": False, "error": "Credential files are excluded."}
    with p.open(encoding="utf-8", errors="replace") as handle:
        text = handle.read(max_chars + 1)
    return {"ok": True, "path": str(p), "content": text[:max_chars], "truncated": len(text) > max_chars}


def create_note(title: str, content: str) -> Dict[str, Any]:
    notes = _safe_path(str(SAFE_ROOTS[0] / "notes"))
    notes.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(c for c in title if c.isalnum() or c in "-_ '").strip() or "note"
    path = notes / f"{safe_title[:100]}.md"
    path = _safe_path(str(path))
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    return {"ok": True, "path": str(path)}


def create_folder(path: str) -> Dict[str, Any]:
    p = _safe_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(p)}


def _load_gui():
    try:
        import pyautogui
        return pyautogui
    except ImportError:
        return None


def type_text(text: str, interval: float = 0.02) -> Dict[str, Any]:
    gui = _load_gui()
    if gui is None:
        return {"ok": False, "error": "GUI control is optional. Run: pip install pyautogui"}
    if not text.isascii():
        return {"ok": False, "error": "This keyboard driver supports ASCII only; no text was typed."}
    gui.write(text, interval=max(0.0, min(float(interval), 0.02)))
    return {"ok": True, "typed_chars": len(text)}


def press_hotkey(keys: list[str]) -> Dict[str, Any]:
    gui = _load_gui()
    if gui is None:
        return {"ok": False, "error": "GUI control is optional. Run: pip install pyautogui"}
    clean = [str(k).lower().strip() for k in keys][:5]
    gui.hotkey(*clean)
    return {"ok": True, "keys": clean}


def mouse_click(x: int, y: int, button: str = "left") -> Dict[str, Any]:
    gui = _load_gui()
    if gui is None:
        return {"ok": False, "error": "GUI control is optional. Run: pip install pyautogui"}
    if button not in {"left", "right", "middle"}:
        button = "left"
    gui.click(x=int(x), y=int(y), button=button)
    return {"ok": True, "x": int(x), "y": int(y), "button": button}


FUNCTIONS = {
    "get_system_status": get_system_status,
    "top_processes": top_processes,
    "open_url": open_url,
    "open_app": open_app,
    "list_files": list_files,
    "search_files": search_files,
    "read_text_file": read_text_file,
    "create_note": create_note,
    "create_folder": create_folder,
    "remember": remember,
    "recall": recall,
    "create_mission": create_mission,
    "list_missions": list_missions,
    "update_mission": update_mission,
    "type_text": type_text,
    "press_hotkey": press_hotkey,
    "mouse_click": mouse_click,
}


def _schema(name, description, properties=None, required=None):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOL_SCHEMAS = [
    _schema("get_system_status", "Read current CPU, RAM, disk, battery, OS and local time."),
    _schema("top_processes", "List processes using the most memory.", {"limit": {"type": "integer", "minimum": 1, "maximum": 25}}),
    _schema("open_url", "Open a website in the user's default browser.", {"url": {"type": "string"}}, ["url"]),
    _schema("open_app", "Open an allowlisted Windows application.", {"name": {"type": "string"}}, ["name"]),
    _schema("list_files", "List files under Desktop, Documents, or Downloads.", {"folder": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["folder"]),
    _schema("search_files", "Search filenames recursively in Desktop, Documents, or Downloads.", {"query": {"type": "string"}, "folder": {"type": "string", "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
    _schema("read_text_file", "Read a safe local text/code file.", {"path": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 100, "maximum": 50000}}, ["path"]),
    _schema("create_note", "Create a new Markdown note in the approved Makma workspace; never overwrite.", {"title": {"type": "string"}, "content": {"type": "string"}}, ["title", "content"]),
    _schema("create_folder", "Create a folder under Desktop, Documents, or Downloads.", {"path": {"type": "string"}}, ["path"]),
    _schema("remember", "Store durable memory only when the user explicitly asks Mak'ma to remember it.", {"content": {"type": "string"}, "kind": {"type": "string"}}, ["content"]),
    _schema("recall", "Search Mak'ma local long-term memory.", {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}),
    _schema("create_mission", "Create a persistent mission/objective for Mak'ma.", {"title": {"type": "string"}, "objective": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"]}}, ["title", "objective"]),
    _schema("list_missions", "List Mak'ma missions.", {"status": {"type": "string", "enum": ["active", "paused", "completed", "failed"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _schema("update_mission", "Update mission status or progress.", {"mission_id": {"type": "integer"}, "status": {"type": "string", "enum": ["active", "paused", "completed", "failed"]}, "progress": {"type": "integer", "minimum": 0, "maximum": 100}}, ["mission_id"]),
    _schema("type_text", "Type text into the currently focused Windows application. Requires explicit HUD approval.", {"text": {"type": "string", "maxLength": 1000}, "interval": {"type": "number", "minimum": 0, "maximum": 0.2}}, ["text"]),
    _schema("press_hotkey", "Press a Windows keyboard shortcut. Requires explicit HUD approval.", {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5}}, ["keys"]),
    _schema("mouse_click", "Click a screen coordinate. Requires explicit HUD approval.", {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right", "middle"]}}, ["x", "y"]),
]



def tool_catalog():
    return [{"name": s["name"], "description": s["description"],
             "parameters": s["parameters"], "risk_level": risk_level(s["name"]),
             "reversible": risk_level(s["name"]) < 2, "timeout_seconds": 30}
            for s in TOOL_SCHEMAS]


def validate_tool(name, args):
    schema = next((s for s in TOOL_SCHEMAS if s["name"] == name), None)
    if schema is None or name not in FUNCTIONS:
        raise ValueError(f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object.")
    errors = list(Draft202012Validator(schema["parameters"]).iter_errors(args))
    if errors:
        raise ValueError("Invalid tool arguments: " + errors[0].message[:300])
    if len(json.dumps(args)) > 60000:
        raise ValueError("Tool arguments exceed the size limit.")
    return args


def _audit(name, args, result):
    from .memory import _conn
    from .store import emit
    # Retain structure and lengths, not raw potentially sensitive file/typed content.
    summary = {key: (f"[{len(value)} characters]" if isinstance(value, str) else value)
               for key, value in args.items()}
    status = "simulated" if result.get("simulated") else "waiting" if result.get("approval_required") else "succeeded" if result.get("ok") else "failed"
    safe_result = {key: result[key] for key in ("ok", "approval_id", "simulated", "verification") if key in result}
    with _conn() as conn:
        conn.execute("INSERT INTO audit_log(tool_name,args_json,risk_level,status,result_json) VALUES (?,?,?,?,?)",
                     (name, json.dumps(summary), risk_level(name), status, json.dumps(safe_result)))
    emit("TOOL_" + status.upper(), f"{name}: {status}", {"tool": name})


def execute_tool(name: str, arguments: str | Dict[str, Any], approved: bool = False,
                 idempotency_key: str | None = None) -> Dict[str, Any]:
    from .store import get_settings
    from .memory import _conn
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        validate_tool(name, args)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    gate = decision(name, approved)
    if gate == "deny":
        result = {"ok": False, "error": "Lockdown is read-only. Change autonomy in Settings to enable actions."}
    elif gate == "approve":
        result = create_approval(name, args)
    elif get_settings().get("simulation") and risk_level(name) > 0:
        result = {"ok": True, "simulated": True, "would_execute": {"tool": name, "args": args},
                  "verification": "Simulation only; no action executed."}
    else:
        key = idempotency_key or str(uuid.uuid4())
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM tool_runs WHERE id=?", (key,)).fetchone()
            if old:
                if old["status"] == "completed":
                    return json.loads(old["result_json"])
                return {"ok": False, "error": "Execution outcome uncertain; inspect activity before retrying.", "uncertain": True}
            conn.execute("INSERT INTO tool_runs(id,tool_name,status) VALUES (?,?,'running')", (key, name))
        try:
            value = FUNCTIONS[name](**args)
            result = value if isinstance(value, dict) else {"ok": True, "items": value}
            result.setdefault("ok", True)
            result.setdefault("verification", "Handler returned observed data." if risk_level(name) == 0 else "Handler completed; see returned evidence.")
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: tool failed. Check arguments and local permissions."}
        with _conn() as conn:
            conn.execute("UPDATE tool_runs SET status='completed',result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), key))
    _audit(name, args, result)
    return result
