import logging
import os
from fastapi import FastAPI, HTTPException
import ansible_runner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Resolve ansible dir relative to this file so it works locally and in Docker
ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansible")


@app.get("/ping")
def ping():
    try:
        # Use host_cwd so playbooks/ and inventory/ are used directly (works locally and in Docker)
        # run_command returns (rc, stdout, stderr) in that order
        rc, out, err = ansible_runner.run_command(
            executable_cmd="ansible-playbook",
            cmdline_args=["ping.yml", "-i", "zos.ini"],
            host_cwd=ANSIBLE_DIR,
        )
        if out:
            logger.info("ansible-playbook stdout:\n%s", out)
        if err:
            logger.error("ansible-playbook stderr:\n%s", err)
        logger.info("ansible-playbook rc=%s", rc)
        return {
            "ok": rc == 0 if rc is not None else False,
            "rc": rc,
            "stdout": out or "",
            "stderr": err or "",
        }
    except Exception as e:
        logger.exception("ping failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
