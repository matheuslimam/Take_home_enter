# worker/run_job.py
import os, time, json, tempfile, uuid, traceback
from supabase import create_client, Client
from typing import List, Dict, Any
from worker.anchors_reading_span import process_pdf_to_json

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET_DOCS = os.environ.get("BUCKET_DOCS", "docs")
BUCKET_RESULTS = os.environ.get("BUCKET_RESULTS", "results")

def _sb() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def _download_pdf_bytes(supabase: Client, path: str) -> bytes:
    # storage3 v2: download retorna bytes diretamente
    data = supabase.storage.from_(BUCKET_DOCS).download(path)
    return data

def _upload_json_result(supabase: Client, job_id: str, file_name: str, result: Dict[str, Any]) -> str:
    # grava em arquivo temporário para evitar o erro do storage3 com BytesIO
    result_bytes = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
    result_rel = f"{job_id}/{uuid.uuid4().hex}-{file_name}.json"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        tmp.write(result_bytes)
        tmp.flush()
        tmp_path = tmp.name

    # contentType só é aceito via headers em alguns adapters, então usamos upload simples
    supabase.storage.from_(BUCKET_RESULTS).upload(result_rel, tmp_path)
    try:
        os.remove(tmp_path)
    except Exception:
        pass
    return result_rel

def _public_result_url(supabase: Client, path: str) -> str:
    # Se o bucket RESULTS for público:
    # new SDK retorna {"data":{"publicUrl":...}}
    res = supabase.storage.from_(BUCKET_RESULTS).get_public_url(path)
    # compat
    if isinstance(res, str):
        return res
    return (res.get("data") or {}).get("publicUrl") or res.get("publicUrl") or ""

def _update_job_counters(supabase: Client, job_id: str):
    # Reconta done/error no banco para atualizar o job
    items = supabase.table("job_items").select("status").eq("job_id", job_id).execute().data or []
    done = sum(1 for it in items if it["status"] == "done")
    err = sum(1 for it in items if it["status"] == "error")
    supabase.table("jobs").update({"done_count": done, "error_count": err}).eq("id", job_id).execute()

    # se terminou, marca job como done (ou error, caso tenha erros e você prefira)
    total = (supabase.table("jobs").select("total_count").eq("id", job_id).single().execute().data or {}).get("total_count", 0)
    if done + err >= total > 0:
        status = "done" if err == 0 else "error"
        supabase.table("jobs").update({"status": status}).eq("id", job_id).execute()

def _process_item(supabase: Client, it: Dict[str, Any]):
    # it: row de job_items
    item_id = it["id"]
    file_name = it["file_name"]
    file_path = it["file_path"]
    schema = it.get("schema") or {}

    start = time.perf_counter()
    try:
        # marca running
        supabase.table("job_items").update({"status": "running", "error_message": None}).eq("id", item_id).execute()

        # baixa pdf
        pdf_bytes = _download_pdf_bytes(supabase, file_path)

        # roda pipeline
        result = process_pdf_to_json(pdf_bytes, schema)

        # sobe json
        result_path = _upload_json_result(supabase, it["job_id"], file_name, result)

        dur_ms = int((time.perf_counter() - start) * 1000)
        supabase.table("job_items").update({
            "status": "done",
            "duration_ms": dur_ms,
            "result_path": result_path,
            "error_message": None
        }).eq("id", item_id).execute()
    except Exception as e:
        dur_ms = int((time.perf_counter() - start) * 1000)
        supabase.table("job_items").update({
            "status": "error",
            "duration_ms": dur_ms,
            "error_message": f"{type(e).__name__}: {e}"
        }).eq("id", item_id).execute()
        traceback.print_exc()

def run_job_id(job_id: str):
    sb = _sb()

    # pega items do job (status != done/error)
    items = (
        sb.table("job_items")
          .select("*")
          .eq("job_id", job_id)
          .neq("status", "done")
          .neq("status", "error")
          .order("created_at")
          .execute()
          .data
        or []
    )

    # se o job estiver em 'queued', passa p/ 'running'
    job_row = sb.table("jobs").select("*").eq("id", job_id).single().execute().data
    if job_row and job_row.get("status") == "queued":
        sb.table("jobs").update({"status": "running"}).eq("id", job_id).execute()

    for it in items:
        _process_item(sb, it)
        _update_job_counters(sb, job_id)
