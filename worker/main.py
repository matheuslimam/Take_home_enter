import os, json, time, asyncio
from typing import Dict, Any, List
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

# -------- env --------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET_DOCS = os.environ.get("BUCKET_DOCS", "docs")
BUCKET_RESULTS = os.environ.get("BUCKET_RESULTS", "results")
WORKER_SECRET = os.environ["WORKER_SECRET"]

# LLM (opcional)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

# -------- supabase client --------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# -------- seu pipeline (copie seu arquivo para a pasta) --------
from anchors_reading_span import process_pdf_to_json

app = FastAPI()

class JobPayload(BaseModel):
    job_id: str

@app.get("/healthz")
def health():
    return {"ok": True}

def _now_iso():
    import datetime as dt
    return dt.datetime.utcnow().isoformat() + "Z"

async def _download_pdf(file_path: str) -> bytes:
    res = supabase.storage.from_(BUCKET_DOCS).download(file_path)
    if res is None:
        raise RuntimeError(f"Failed to download: {file_path}")
    return res

async def _upload_json(path: str, obj: Any):
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    supabase.storage.from_(BUCKET_RESULTS).upload(path, data, {
        "content-type": "application/json",
        "upsert": "true",
    })

async def _process_item(it: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    supabase.table("job_items").update({"status": "running", "error_message": None}).eq("id", it["id"]).execute()

    pdf_bytes = await _download_pdf(it["file_path"])
    schema = it.get("schema") or {}
    result_obj = process_pdf_to_json(pdf_bytes, schema)

    result_path = f"{it['job_id']}/{it['id']}.json"
    await _upload_json(result_path, result_obj)

    dur_ms = int((time.perf_counter() - t0) * 1000)
    supabase.table("job_items").update({
        "status": "done",
        "duration_ms": dur_ms,
        "result_path": result_path
    }).eq("id", it["id"]).execute()

    return {"id": it["id"], "ms": dur_ms}

async def _run_job(job_id: str, concurrency: int = 3) -> Dict[str, Any]:
    supabase.table("jobs").update({"status": "running", "updated_at": _now_iso()}).eq("id", job_id).execute()

    r = supabase.table("job_items").select("*").eq("job_id", job_id).order("created_at", desc=False).execute()
    items: List[Dict[str, Any]] = r.data or []
    if not items:
        supabase.table("jobs").update({"status": "done", "updated_at": _now_iso()}).eq("id", job_id).execute()
        return {"ok": True, "processed": 0}

    done = err = 0
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def worker(it):
        nonlocal done, err
        async with sem:
            try:
                out = await _process_item(it)
                done += 1
                supabase.table("jobs").update({
                    "done_count": done, "error_count": err, "updated_at": _now_iso()
                }).eq("id", job_id).execute()
                results.append(out)
            except Exception as e:
                err += 1
                supabase.table("job_items").update({
                    "status": "error", "error_message": str(e)
                }).eq("id", it["id"]).execute()
                supabase.table("jobs").update({
                    "error_count": err, "updated_at": _now_iso()
                }).eq("id", job_id).execute()

    await asyncio.gather(*(worker(it) for it in items))

    supabase.table("jobs").update({
        "status": "error" if err else "done",
        "updated_at": _now_iso()
    }).eq("id", job_id).execute()

    return {"ok": True, "processed": len(items), "done": done, "error": err, "items": results}

@app.post("/process-job")
async def process_job(req: Request, payload: JobPayload):
    if req.headers.get("x-worker-secret") != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    result = await _run_job(payload.job_id, concurrency=3)
    return JSONResponse(result)
