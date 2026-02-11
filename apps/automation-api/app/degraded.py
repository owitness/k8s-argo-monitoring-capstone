from fastapi import FastAPI, HTTPException
import uuid
import boto3
import subprocess
import sys
import tempfile
import os
import json
import logging
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("automation-api")

app = FastAPI(title="Automation Control Plane - Ansible Runner")

S3_BUCKET = "ansible-playbook-s3-dae"
s3 = boto3.client("s3")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-jcl")
def run_jcl(
    playbook_name: str = "create_hamlet_jcl.yml",
    jcl_file: str = "GENER3",
    s3_prefix: str = "",
):
    prefix = f"{s3_prefix}/".lstrip("/") if s3_prefix else ""
    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] run-jcl: playbook={playbook_name} jcl_file={jcl_file}")

    try:
        with tempfile.TemporaryDirectory() as d:
            # Download from S3 (paths match repo: ansible/playbooks/, ansible/jcl/)
            logger.info(f"[{job_id}] Downloading playbook, JCL, and key from S3")
            s3.download_file(S3_BUCKET, f"{prefix}ansible/playbooks/{playbook_name}", os.path.join(d, playbook_name))
            s3.download_file(S3_BUCKET, f"{prefix}ansible/jcl/{jcl_file}", os.path.join(d, jcl_file))
            s3.download_file(S3_BUCKET, "mainframe_key.pem", key_path := os.path.join(d, "mainframe_key.pem"))
            os.chmod(key_path, 0o600)

            # Inventory (matches ansible/playbooks/create_hamlet_jcl.yml and deployment zos-var)
            inv = {
                "all": {"children": {"zos": {"hosts": {"mainframe": {
                    "ansible_host": "67.217.62.83",
                    "ansible_user": "GAMA12",
                    "ansible_connection": "ssh",
                    "ansible_ssh_private_key_file": key_path,
                    "ansible_ssh_common_args": "-o StrictHostKeyChecking=no",
                    "ansible_python_interpreter": "/usr/lpp/IBM/cyp/v3r11/pyz/bin/python",
                    "ansible_pipelining": True,
                    "ansible_remote_tmp": "/tmp/ansible-gama12",
                }}}}}}
            inv_path = os.path.join(d, "inventory.yml")
            with open(inv_path, "w") as f:
                yaml.dump(inv, f)

            # Run playbook. Use file handles instead of pipes for stdout/stderr to avoid
            # Ansible's os.get_blocking() failing on Windows when FDs are pipes.
            logger.info(f"[{job_id}] Executing ansible-playbook")
            stdout_path = os.path.join(d, "stdout.txt")
            stderr_path = os.path.join(d, "stderr.txt")
            with open(stdout_path, "w") as fo, open(stderr_path, "w") as fe:
                proc = subprocess.run(
                    [sys.executable, "-m", "ansible.cli.playbook", "-i", inv_path, playbook_name, "-e", f"jcl_file={jcl_file}"],
                    stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                    timeout=1800, cwd=d
                )
            with open(stdout_path) as f:
                stdout = f.read()
            with open(stderr_path) as f:
                stderr = f.read()
            logger.info(f"[{job_id}] Playbook completed with exit_code={proc.returncode}")

            # Read JCL result
            jcl_result = None
            result_file = os.path.join(d, "jcl_result.json")
            if os.path.exists(result_file):
                try:
                    with open(result_file) as f:
                        jcl_result = json.load(f)
                except json.JSONDecodeError as e:
                    logger.warning(f"[{job_id}] Could not parse jcl_result.json: {e}")

            return {
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                "jcl_file": jcl_file,
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                **({"jcl_result": jcl_result} if jcl_result else {}),
            }

    except subprocess.TimeoutExpired:
        logger.error(f"[{job_id}] Playbook timed out")
        raise HTTPException(408, "Playbook timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{job_id}] Error in run-jcl: {e}")
        raise HTTPException(500, str(e))
