from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uuid
import boto3
import subprocess
import tempfile
import os
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
    
    If extra_vars contains complex types (dicts), they will be saved to a vars file
    and passed to ansible using -e @varsfile.yml
    """
    execution_id = str(uuid.uuid4())[:8]
    logger.info(f"[{execution_id}] Starting playbook execution")
    
    try:
        # Build ansible-playbook command
        command = [
            "ansible-playbook",
            "-i", inventory_path,
            playbook_path
        ]
        
        # Execute the playbook
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout
        )
        
        success = result.returncode == 0
        logger.info(f"[{execution_id}] Playbook execution completed with exit code: {result.returncode}")
        
        return {
            "execution_id": execution_id,
            "success": success,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr
        }
    
    except subprocess.TimeoutExpired:
        logger.error(f"[{execution_id}] Playbook execution timed out")
        raise HTTPException(
            status_code=408,
            detail="Playbook execution timed out after 30 minutes"
        )
    except Exception as e:
        logger.error(f"[{execution_id}] Error executing playbook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Playbook execution failed: {str(e)}")

# JCL Endpoint
@app.post("/run-jcl")
async def run_jcl(
    playbook_name: str = "ansible/create_hamlet_jcl.yml",
    inventory_name: str = "inventory.yml",
    jcl_file: str = "GENER3",
    s3_prefix: str = "",
):
    """
    Run an Ansible playbook downloaded from S3, along with JCL file and variables
    
    Parameters:
    - playbook_name: Name of the playbook file in S3 (e.g., ansible/create_hamlet_jcl.yml)
    - inventory_name: Name of the inventory file in S3 (e.g., inventory.yml)
    - jcl_file: JCL file name in jcl/ folder (e.g., GENER3)
    - s3_prefix: S3 path prefix (e.g., "ansible-files/") - optional
    - extra_vars: Extra variables to pass to ansible-playbook (e.g., {"target_host": "zos1"})
    
    Note: Automatically loads var.yml from S3 bucket root if it exists.
    Variables from var.yml are passed to the playbook as environment_vars and other variables.
    extra_vars parameter takes precedence over var.yml values.
    """
    job_id = str(uuid.uuid4())[:8]
    
    try:
            # Create temporary directory for downloaded files
        with tempfile.TemporaryDirectory() as tmpdir:
            logger.info(f"[{job_id}] Created temporary directory: {tmpdir}")

            # Create subdirectories to maintain structure
            jcl_tmpdir = os.path.join(tmpdir, "jcl")
            os.makedirs(jcl_tmpdir, exist_ok=True)

            # Construct S3 paths
            playbook_s3_path = f"{s3_prefix}{playbook_name}".lstrip("/")
            inventory_s3_path = f"{s3_prefix}{inventory_name}".lstrip("/")
            jcl_s3_path = f"{s3_prefix}jcl/{jcl_file}".lstrip("/")

            # Local paths
            playbook_local = os.path.join(tmpdir, os.path.basename(playbook_name))
            inventory_local = os.path.join(tmpdir, inventory_name)
            jcl_local = os.path.join(jcl_tmpdir, jcl_file)

            # Download playbook from S3
            logger.info(f"[{job_id}] Downloading playbook from S3: {playbook_s3_path}")
            download_from_s3(S3_BUCKET, playbook_s3_path, playbook_local)

            # Download inventory from S3
            logger.info(f"[{job_id}] Downloading inventory from S3: {inventory_s3_path}")
            download_from_s3(S3_BUCKET, inventory_s3_path, inventory_local)

            # Download JCL file from S3
            logger.info(f"[{job_id}] Downloading JCL file from S3: {jcl_s3_path}")
            download_from_s3(S3_BUCKET, jcl_s3_path, jcl_local)

            # Copy the JCL file to where the playbook expects it
            target_dir = os.path.join(os.getcwd(), "../jcl")
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, jcl_file)

            shutil.copy(jcl_local, target_path)
            logger.info(f"[{job_id}] Copied JCL to {target_path}")

            # Now run your playbook
            logger.info(f"[{job_id}] Executing ansible-playbook")
            execution_result = await execute_ansible_playbook(
                playbook_local,
                inventory_local,
                tmpdir  # Pass tmpdir so vars can be written there
            )
            
            return JSONResponse({
                "job_id": job_id,
                "status": "completed",
                "playbook": playbook_name,
                "inventory": inventory_name,
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