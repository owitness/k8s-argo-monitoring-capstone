from fastapi import FastAPI, HTTPException
import ansible_runner

app = FastAPI()

ANSIBLE_DIR = "/app/ansible"


@app.get("/ping")
def ping():
    try:
        r = ansible_runner.run(private_data_dir=ANSIBLE_DIR, playbook="ping.yaml")
        return {"ok": r.rc == 0, "rc": r.rc}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
