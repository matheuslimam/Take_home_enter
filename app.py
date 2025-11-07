# app.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from worker.run_job import run_job_id  # sua função existente

app = FastAPI()

# Origens permitidas (sem path)
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "https://matheuslimam.github.io/Take_home_enter",  # GH Pages
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,               # deixe False se não usa cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=86400,
)

class JobBody(BaseModel):
    job_id: str

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/process-job")
def process_job(body: JobBody):
    try:
        # roda seu worker síncrono. Se for demorado, considere colocar em thread/task queue.
        run_job_id(body.job_id)
        return {"ok": True, "job_id": body.job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
