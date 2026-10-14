"""Closed read-only library and template commands; effectful calls remain gated."""
from dataclasses import asdict
import json
import re
from ..core import library, prompts
from ..core.transcription_job_application import LocalTranscriptionJobError

CAPABILITIES = frozenset({"LIB-01", "LIB-02", "COPY-07"})


def execute(argv, runtime):
    if argv[-2:] != ["--output", "json"]:
        raise LocalTranscriptionJobError("INVALID_ARGUMENT", "JSON_OUTPUT_REQUIRED")
    prefix = tuple(argv[:3]) if argv[:2] == ["copy", "templates"] else tuple(argv[:2])
    profiles = {
        ("library", "list"): ("LIB-01", {"--kind", "--search", "--folder-id", "--sort", "--offset", "--limit"}),
        ("library", "show"): ("LIB-02", {"--entry-id"}),
        ("copy", "templates", "list"): ("COPY-07", {"--direction"}),
        ("copy", "templates", "show"): ("COPY-07", {"--direction", "--template-id"}),
    }
    if prefix not in profiles:
        raise LocalTranscriptionJobError("CAPABILITY_UNAVAILABLE", "FORMAL_CLI_NOT_IMPLEMENTED")
    capability, allowed = profiles[prefix]
    options = {}
    tail = argv[len(prefix):-2]
    if len(tail) % 2:
        raise LocalTranscriptionJobError("INVALID_ARGUMENT", "OPTION_VALUE_REQUIRED")
    for name, value in zip(tail[::2], tail[1::2]):
        if name not in allowed:
            raise LocalTranscriptionJobError("UNKNOWN_FIELD", "OPTION_NOT_REGISTERED")
        key = name[2:].replace("-", "_")
        if key in options or value.startswith("--"):
            raise LocalTranscriptionJobError("INVALID_ARGUMENT", "OPTION_VALUE_INVALID")
        options[key] = value
    runtime._application.assert_library_read_allowed(capability)
    try:
        if prefix == ("library", "list"):
            for key in ("limit", "offset"):
                if key in options:
                    options[key] = int(options[key])
            return library.query_entries(**options)
        if prefix == ("library", "show"):
            eid = options.get("entry_id", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", eid) is None or eid == "folders":
                raise ValueError("entry id")
            path = library.LIBRARY_DIR / f"{eid}.json"
            if path.is_symlink():
                raise LocalTranscriptionJobError("UNSAFE_PATH", "LIBRARY_RECORD_SYMLINK")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("id") != eid:
                raise ValueError("entry shape")
            if data.get("kind", "transcript") not in {"transcript", "rewrite", "voice"}:
                raise FileNotFoundError("retired entry")
            if data.get("kind", "transcript") in {"transcript", "rewrite"}:
                md = library._markdown_abs(data.get("markdown_path"), eid)
                if md.is_file():
                    data["text"] = md.read_text(encoding="utf-8")
                data["content_hash"] = library._content_hash(data.get("text", ""))
            return {"entry": data}
        from ..config import AppConfig
        direction = options.get("direction", "rewrite")
        if direction not in prompts.TEMPLATES:
            raise ValueError("direction")
        templates = [asdict(t) for t in prompts.templates_for(direction)]
        custom = AppConfig.load().copywriting_user_templates.get(direction, [])
        templates += [{"id": t["id"], "name": t["name"], "direction": direction,
                       "system_text": t["text"], "origin": "user"}
                      for t in custom if isinstance(t, dict) and all(isinstance(t.get(k), str) for k in ("id", "name", "text"))]
        if prefix[-1] == "show":
            found = next((t for t in templates if t["id"] == options.get("template_id")), None)
            if found is None:
                raise FileNotFoundError("template")
            return {"template": found}
        return {"templates": templates}
    except FileNotFoundError as exc:
        raise LocalTranscriptionJobError("NOT_FOUND", "LIBRARY_ITEM_NOT_FOUND") from exc
    except (TypeError, ValueError, UnicodeError, KeyError) as exc:
        raise LocalTranscriptionJobError("INVALID_ARGUMENT", "LIBRARY_REQUEST_INVALID") from exc
    except OSError as exc:
        raise LocalTranscriptionJobError("INTERNAL_ERROR", "LIBRARY_READ_FAILED") from exc
