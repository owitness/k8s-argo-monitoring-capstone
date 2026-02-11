from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uuid
import boto3
import subprocess
import tempfile
import os
import stat
import shutil
import logging
import yaml
from jinja2 import Environment, BaseLoader
from pathlib import Path
from typing import Optional, Dict

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("automation-api")

app = FastAPI(title="Automation Control Plane - Ansible Runner")

# S3 Configuration
S3_BUCKET = "ansible-playbook-s3-dae"
s3_client = boto3.client('s3')

# Health Endpoint
@app.get("/health")
async def health():
    return {"status": "ok"}


def download_from_s3(bucket: str, s3_path: str, local_path: str) -> bool:
    """Download a file from S3 to local filesystem"""
    try:
        logger.info(f"Downloading s3://{bucket}/{s3_path} to {local_path}")
        s3_client.download_file(bucket, s3_path, local_path)
        logger.info(f"Successfully downloaded {s3_path}")
        return True
    except Exception as e:
        logger.error(f"Error downloading from S3: {str(e)}")
        raise

async def execute_ansible_playbook(
    playbook_path: str,
    inventory_path: str,
    tmpdir: Optional[str] = None
) -> Dict:
    """
    Execute Ansible playbook and capture output
    """
    execution_id = str(uuid.uuid4())[:8]
    logger.info(f"[{execution_id}] Starting playbook execution")
    try:
        # Create ansible.cfg in tmpdir
        ansible_cfg_path = os.path.join(tmpdir, 'ansible.cfg')
        with open(ansible_cfg_path, 'w') as f:
            f.write("""[defaults]
forks = 25
environment = LANG=en_US.UTF-8,LC_ALL=en_US.UTF-8
host_key_checking = False

[ssh_connection]
pipelining = True
""")
        # Change working directory to tmpdir
        cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            # Build extra vars string
            extra_vars = "ansible_remote_tmp=/tmp/ansible_tmp ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
            # Build command
            command = [
                "ansible-playbook",
                "-i", inventory_path,
                playbook_path,
                "-e", extra_vars
            ]
            # Run subprocess in tmpdir
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800
            )
        finally:
            os.chdir(cwd)

        success = result.returncode == 0
        logger.info(f"[{execution_id}] Playbook completed with exit code: {result.returncode}")
        return {
            "execution_id": execution_id,
            "success": success,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    except subprocess.TimeoutExpired:
        logger.error(f"[{execution_id}] Playbook timed out")
        raise HTTPException(
            status_code=408,
            detail="Playbook timed out after 30 minutes"
        )
    except Exception as e:
        logger.error(f"[{execution_id}] Error executing playbook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Playbook execution failed: {str(e)}")


# JCL Endpoint
@app.post("/run-jcl")
async def run_jcl(
    playbook_name: str = "create_hamlet_jcl.yml",
    #inventory_name: str = "inventory.yml",
    jcl_file: str = "GENER3",
    s3_prefix: str = "",
):
    job_id = str(uuid.uuid4())[:8]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f"[{job_id}] Created temporary directory: {tmpdir}")

            # Download playbook
            playbook_s3_path = f"{s3_prefix}ansible/{playbook_name}".lstrip("/")
            playbook_local = os.path.join(tmpdir, os.path.basename(playbook_name))
            download_from_s3(S3_BUCKET, playbook_s3_path, playbook_local)

            # Download inventory
            #inventory_s3_path = f"{s3_prefix}{inventory_name}".lstrip("/")
            #inventory_local = os.path.join(tmpdir, inventory_name)
            #download_from_s3(S3_BUCKET, inventory_s3_path, inventory_local)

            # Download JCL file
            jcl_s3_path = f"{s3_prefix}jcl/{jcl_file}".lstrip("/")
            jcl_local = os.path.join(tmpdir, jcl_file)
            download_from_s3(S3_BUCKET, jcl_s3_path, jcl_local)

            # Download the private key from S3
            key_s3_path = "mainframe_key.pem"  # Adjust as needed
            local_key_path = os.path.join(tmpdir, "mainframe_key.pem")
            download_from_s3(S3_BUCKET, key_s3_path, local_key_path)

            os.chmod(local_key_path, stat.S_IRUSR | stat.S_IWUSR)

            inventory_dict = {
                'all': {
                    'children': {
                        'zos': {
                            'hosts': {
                                'mainframe': {
                                    'ansible_host': '67.217.62.83',  # your host
                                    'ansible_user': 'GAMA12',
                                    'ansible_python_interpreter': '/usr/lpp/IBM/cyp/v3r11/pyz/bin/python',
                                    'ansible_ssh_private_key_file': local_key_path
                                }
                            }
                        }
                    }
                }
            }

            # Save the generated inventory
            inventory_path = os.path.join(tmpdir, 'inventory.yml')
            with open(inventory_path, 'w') as f:
                yaml.dump(inventory_dict, f)


            target_dir = os.path.join(os.getcwd(), "../jcl")
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, jcl_file)
            shutil.copy(jcl_local, target_path)
            logger.info(f"[{job_id}] Copied JCL to {target_path}")

            # Run the playbook with the generated inventory
            logger.info(f"[{job_id}] Executing ansible-playbook")
            execution_result = await execute_ansible_playbook(
                playbook_local,
                inventory_path,
                tmpdir
            )

            # Return success response
            return JSONResponse({
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                #"inventory": inventory_name,
                "jcl_file": jcl_file,
                "jcl_local_path": jcl_local,
                "s3_bucket": S3_BUCKET,
                "success": execution_result["success"],
                "exit_code": execution_result["exit_code"],
                "execution_id": execution_result["execution_id"],
                "stdout": execution_result["stdout"],
                "stderr": execution_result["stderr"]
            })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{job_id}] Error in run-jcl: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={
                "job_id": job_id,
                "status": "failed",
                "error": str(e)
            }
        )