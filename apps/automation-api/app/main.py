import json
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
        }
        if job_load_error is not None:
            result["job_load_error"] = job_load_error
        return result
    except Exception as e:
        logger.exception("run-jcl failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
