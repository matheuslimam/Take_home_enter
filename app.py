# app.py
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from worker.run_job import run_job_id

app = FastAPI()

# CORS: ajuste conforme seus domínios
origins = [
    "http://localhost:5173",
    "https://matheuslimam.github.io/Take_home_enter",        # GH Pages (ajuste!)
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in origins if o],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class JobBody(BaseModel):
    job_id: str

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/process-job")
def process_job(body: JobBody):
    try:
        run_job_id(body.job_id)
        return {"ok": True, "job_id": body.job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
