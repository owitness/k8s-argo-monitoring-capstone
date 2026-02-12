import json
import logging
import os
import uuid
from datetime import datetime
from fastapi import FastAPI, HTTPException
import ansible_runner
from prometheus_fastapi_instrumentator import Instrumentator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# Resolve ansible dir relative to this file so it works locally and in Docker
ANSIBLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansible")

# In-memory last JCL run summary (job_id, success, timestamp); cleared on restart
_last_jcl_run: dict | None = None


@app.get("/ping")
def ping():
    try:
        # Use host_cwd so playbooks/ and inventory/ are used directly (works locally and in Docker)
        # run_command returns (stdout, stderr, rc) per ansible_runner.interface
        out, err, rc = ansible_runner.run_command(
            executable_cmd="ansible-playbook",
            cmdline_args=["ping.yml", "-i", "zos.yaml"],
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


@app.post("/run-jcl")
def run_jcl():
    job_id = uuid.uuid4().hex[:8]
    try:
        out, err, rc = ansible_runner.run_command(
            executable_cmd="ansible-playbook",
            cmdline_args=["create_hamlet_jcl.yml", "-i", "zos.yaml"],
            host_cwd=ANSIBLE_DIR,
        )
        if out:
            logger.info("ansible-playbook stdout:\n%s", out)
        if err:
            logger.error("ansible-playbook stderr:\n%s", err)
        logger.info("ansible-playbook rc=%s", rc)

        ok = rc == 0 if rc is not None else False
        job = None
        job_load_error = None

        if ok:
            jcl_result_path = os.path.join(ANSIBLE_DIR, "jcl_result.json")
            if os.path.exists(jcl_result_path):
                try:
                    with open(jcl_result_path, "r") as f:
                        job = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Could not parse jcl_result.json: %s", e)
                    job_load_error = str(e)
            else:
                logger.warning("jcl_result.json not found after playbook success")
                job_load_error = "jcl_result.json not found"

        result = {
            "ok": ok,
            "rc": rc,
            "stdout": out or "",
            "stderr": err or "",
            "job": job,
            "job_id": job_id,
        }
        if job_load_error is not None:
            result["job_load_error"] = job_load_error
        global _last_jcl_run
        _last_jcl_run = {
            "job_id": job_id,
            "success": ok,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }
        return result
    except Exception as e:
        logger.exception("run-jcl failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jcl/status")
def jcl_status():
    """Return last JCL run summary (job_id, success, timestamp)."""
    if _last_jcl_run is not None:
        return _last_jcl_run
    return {
        "job_id": None,
        "success": None,
        "timestamp": None,
        "message": "No run yet",
    }


@app.post("/re-run-jcl")
def re_run_jcl():
    """Re-run the same JCL workflow as POST /run-jcl; same response shape."""
    return run_jcl()
