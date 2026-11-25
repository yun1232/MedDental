"""Bounded JSON subprocess seam. Harness owns all model calls and agent lifecycles."""
import base64
import json
import os
import sys
from store import MemoryStore, settings_from_env
from schemas import MemoryError
from team_memory import MedicalProject


def main():
    store = project = None
    try:
        raw = sys.stdin.buffer.read(36 * 1024 * 1024 + 1)
        if len(raw) > 36 * 1024 * 1024:
            raise MemoryError("input_limit", "Medical bridge input is too large")
        request = json.loads(raw)
        store = MemoryStore(os.environ["ORAL_MEMORY_DB"], os.environ["ORAL_MEMORY_PATIENT_ID"],
                            os.environ.get("ORAL_MEMORY_ACTOR_ROLE", "patient"), settings=settings_from_env(os.environ))
        project = MedicalProject(store)
        op, args = request["op"], request.get("args", {})
        operations = {"state": project.state, "ingest": project.ingest, "context": project.context,
                      "save_output": project.save_output, "retrieve": project.retrieve,
                      "make_views": project.make_views, "finish": project.finish,
                      "control": project.control, "save_baseline": project.save_baseline}
        if op == "image":
            value = {"data": base64.b64encode(project.image(**args)).decode(), "media_type": "image/png"}
        elif op in operations:
            value = operations[op](**args)
        else:
            raise MemoryError("unknown_operation", "Operation is not available to the patient team")
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=False, allow_nan=False))
    except MemoryError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
    except (ValueError, TypeError, KeyError):
        print(json.dumps({"ok": False, "code": "invalid_input", "message": "Invalid bridge request"}))
    except Exception:
        print(json.dumps({"ok": False, "code": "storage_error", "message": "Medical storage operation failed"}))
    finally:
        if project:
            project.close()
        if store:
            store.close()


if __name__ == "__main__":
    main()
