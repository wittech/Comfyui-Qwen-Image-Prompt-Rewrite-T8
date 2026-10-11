"""Run via runpy inside Linux ComfyUI after custom nodes load; exercise 20 cycles."""

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pe_runtime import (DEFAULT_EDIT, DEFAULT_MMPROJ, DEFAULT_T2I,
                        SERVER, resolve_model)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "runtime" / "lifecycle-results.jsonl"
PROFILES = [
    ("t2i", DEFAULT_T2I, None, 16384),
    ("edit", DEFAULT_EDIT, DEFAULT_MMPROJ, 32768),
    ("heretic", "pe_t2i_heretic-Q4_K_M.gguf", None, 16384),
    ("t2i-again", DEFAULT_T2I, None, 16384),
]


def gpu_mb():
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=10)
        return int(output.splitlines()[0].strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


REPORT.parent.mkdir(parents=True, exist_ok=True)
with REPORT.open("w", encoding="utf-8") as stream:
    try:
        for cycle in range(1, 21):
            prior = None
            for label, model_name, vision_name, context in PROFILES:
                started = time.monotonic()
                model = resolve_model(model_name)
                vision = resolve_model(vision_name, True) if vision_name else None
                SERVER.start(model, vision, context, 99)
                current = SERVER.llm
                assert current is not None and SERVER.storage.llm is current
                assert prior is None or prior is not current, "previous model was not replaced"
                current_id = id(current)
                SERVER.start(model, vision, context, 99)
                assert SERVER.llm is current, "same profile did not reuse the model"
                record = {"cycle": cycle, "profile": label, "model_id": current_id,
                          "elapsed_seconds": round(time.monotonic() - started, 2),
                          "gpu_mb": gpu_mb()}
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                print(json.dumps(record), flush=True)
                prior = current
            SERVER.stop()
            assert SERVER.llm is None, "explicit unload left a model loaded"
            record = {"cycle": cycle, "profile": "unloaded", "gpu_mb": gpu_mb()}
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            print(json.dumps(record), flush=True)
    finally:
        SERVER.stop()
