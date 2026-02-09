from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uuid

app = FastAPI(title="Automation Control Plane - Barebones")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/run-jcl")
async def run_jcl(jcl_name: str = "hello.jcl"):
    job_id = str(uuid.uuid4())[:8]
    # In real version: create Kubernetes Job here using kubernetes client
    # For barebones: just simulate
    return JSONResponse({
        "message": "Job triggered (simulated)",
        "job_id": job_id,
        "jcl": jcl_name,
        "status": "pending"
    })