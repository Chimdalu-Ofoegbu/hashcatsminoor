#!/usr/bin/env python3
"""A stand-in for the vastai CLI used by the tests. State lives in $FAKE_VAST_STATE (JSON)."""
import json
import os
import sys

path = os.environ["FAKE_VAST_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {"log": [], "shows": 0, "destroyed": False}
argv = [a for a in sys.argv[1:] if a != "--raw"]
state["log"].append(argv)
cmd = " ".join(argv[:2])
out = ""
if cmd == "show user":
    out = json.dumps({"email": "you@example.com", "credit": 30.0})
elif cmd == "search offers":
    out = json.dumps([
        {"id": 111, "dph_total": 0.35, "gpu_name": "RTX 4090", "num_gpus": 1, "cuda_max_good": 12.8,
         "reliability2": 0.99, "geolocation": "SE", "inet_down": 800, "disk_space": 100},
        {"id": 222, "dph_total": 0.30, "gpu_name": "RTX 4090", "num_gpus": 1, "cuda_max_good": 12.6,
         "reliability2": 0.98, "geolocation": "NL", "inet_down": 500, "disk_space": 60},
    ])
elif cmd == "create instance":
    out = "{'success': True, 'new_contract': 4242}"
elif cmd == "show instance":
    state["shows"] += 1
    status = "loading" if state["shows"] < 2 else "running"
    out = json.dumps({"id": 4242, "actual_status": status, "dph_total": 0.30})
elif argv[:1] == ["ssh-url"]:
    out = "ssh://root@203.0.113.5:40123"
elif cmd == "destroy instance":
    state["destroyed"] = True
    out = json.dumps({"success": True})
else:
    sys.stderr.write("unknown command\n")
    json.dump(state, open(path, "w"))
    sys.exit(2)
json.dump(state, open(path, "w"))
print(out)
