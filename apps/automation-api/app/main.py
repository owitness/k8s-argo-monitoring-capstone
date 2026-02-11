import os
from fastapi import FastAPI, HTTPException
import ansible_runner

app = FastAPI()

# Resolve ansible dir relative to this file so it works locally and in Docker
ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansible")


@app.get("/ping")
def ping():
    try:
        # Use host_cwd so playbooks/ and inventory/ are used directly (works locally and in Docker)
        out, err, rc = ansible_runner.run_command(
            executable_cmd="ansible-playbook",
            cmdline_args=["playbooks/ping.yaml", "-i", "inventory/zos.ini"],
            host_cwd=ANSIBLE_DIR,
        )
        return {"ok": rc == 0, "rc": rc}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
