import os, time, io, json, traceback
from datetime import datetime
from supabase import create_client, Client
import requests

# IMPORTA a função integrada do extractor (veja seção B)
from anchors_reading_span import process_pdf_to_json

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
BUCKET_DOCS = os.environ.get("BUCKET_DOCS", "docs")
BUCKET_RESULTS = os.environ.get("BUCKET_RESULTS", "results")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "2"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "3"))

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Env SUPABASE_URL / SUPABASE_SERVICE_KEY ausentes")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def now_ms():
    return int(time.time() * 1000)




def get_public_url(bucket: str, path: str) -> str:
    # buckets públicos: ok (para privados, use signed url)
    data = supabase.storage.from_(bucket).get_public_url(path)
    if isinstance(data, dict):
        # supabase-py v2: retorna {"data": {"publicUrl": "..."}}
        url = data.get("data", {}).get("publicUrl")
    else:
        # fallback antigo
        url = data.public_url
    if not url:
        raise RuntimeError(f"Sem public URL para {bucket}/{path}")
    return url

# ADD perto do topo, após imports
def parse_schema(schema_raw):
    # Aceita dict, None, string JSON
    if not schema_raw:
        return {}
    if isinstance(schema_raw, dict):
        return schema_raw
    if isinstance(schema_raw, str):
        try:
            return json.loads(schema_raw)
        except Exception:
            # Se vier inválido, volta vazio (não quebra o worker)
            return {}
    return {}

def claim_item(item_id: str) -> bool:
    """
    Tenta mudar status de 'queued' para 'running' de forma otimista.
    Retorna True se conseguiu (item ficou 'running'), False caso contrário.
    """
    try:
        res = supabase.table("job_items")\
            .update({"status": "running"})\
            .eq("id", item_id)\
            .eq("status", "queued")\
            .execute()
        # Em supabase-py v2, res.data contém as linhas alteradas
        return bool(res.data)
    except Exception as e:
        print("[worker] claim_item failed:", e)
        return False

from urllib.parse import quote

def get_public_url(bucket: str, path: str) -> str:
    """
    Buckets públicos: podemos construir a URL diretamente e evitar
    variações do supabase-py (dict, objeto, string etc).
    """
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL ausente")
    base = SUPABASE_URL.rstrip("/")
    # Garante encoding seguro (mantém '/' do path)
    return f"{base}/storage/v1/object/public/{bucket}/{quote(path, safe='/')}"


def download_pdf(bucket: str, path: str) -> bytes:
    url = get_public_url(bucket, path)
    r = requests.get(url, timeout=60, headers={"User-Agent": "takehome-worker/1.0"})
    r.raise_for_status()
    return r.content


import io, os, json, tempfile

def upload_json_result(job_id: str, file_name: str, payload: dict) -> str:
    """
    Faz upload do JSON gerando um arquivo temporário (compatível com storage3 sync).
    """
    # caminho padronizado no bucket
    stem = file_name.rsplit(".", 1)[0]
    out_path = f"{job_id}/{stem}.json"

    data_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    # Escreve em arquivo temporário e passa o PATH para o upload()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as tmp:
            tmp.write(data_bytes)
            tmp.flush()
            tmp_path = tmp.name

        res = supabase.storage.from_(BUCKET_RESULTS).upload(
            path=out_path,
            file=tmp_path,  # <- passa o caminho, não BytesIO
            file_options={
                # essas chaves funcionam no storage3 (headers são mapeados internamente)
                "content-type": "application/json",
                "cache-control": "3600",
                "upsert": "true",
            },
        )

        # Trata possíveis formatos de retorno (string/dict/obj)
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res["error"]["message"])

        return out_path
    finally:
        # limpa o arquivo temporário
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass


def update_job_progress(job_id: str):
    ji = supabase.table("job_items").select("status").eq("job_id", job_id).execute().data or []
    total = len(ji)
    done = sum(1 for r in ji if r["status"] == "done")
    err  = sum(1 for r in ji if r["status"] == "error")
    status = "running"
    if total > 0 and done + err == total:
        status = "done" if err == 0 else "error"
    supabase.table("jobs").update({
        "total_count": total,
        "done_count": done,
        "error_count": err,
        "status": status
    }).eq("id", job_id).execute()

def safe_update_item(item_id: str, patch: dict):
    try:
        supabase.table("job_items").update(patch).eq("id", item_id).execute()
    except Exception as e:
        # último recurso: logar erro sem quebrar o loop
        print("[worker] update item failed:", e)

def process_one(item: dict):
    start = now_ms()
    item_id = item["id"]
    job_id = item["job_id"]
    file_name = item["file_name"]
    file_path = item["file_path"]
    schema = parse_schema(item.get("schema"))

    try:
        # marca 'running' somente se ainda estiver 'queued'
        if not claim_item(item_id):
            # outro worker pode ter pegado; apenas retorna
            return

        # 1) baixa PDF
        pdf_bytes = download_pdf(BUCKET_DOCS, file_path)

        # 2) roda extractor integrado
        result = process_pdf_to_json(pdf_bytes, schema) or {}

        # 3) sobe JSON para o bucket results
        result_path = upload_json_result(job_id, file_name, result)

        dur = now_ms() - start
        safe_update_item(item_id, {
            "status": "done",
            "duration_ms": dur,
            "result_path": result_path,
            "error_message": None
        })

    except Exception as e:
        dur = now_ms() - start
        msg = f"{type(e).__name__}: {e}"
        print("[worker] ERROR process_one:\n", traceback.format_exc())
        safe_update_item(item_id, {
            "status": "error",
            "duration_ms": dur,
            "error_message": msg
        })

    finally:
        update_job_progress(job_id)


def main_loop():
    print("[worker] started")
    while True:
        try:
            resp = supabase.table("job_items").select("*").eq("status", "queued").limit(BATCH_SIZE).execute()
            items = resp.data or []
            if not items:
                time.sleep(POLL_INTERVAL)
                continue
            for it in items:
                process_one(it)
        except KeyboardInterrupt:
            print("[worker] stop requested")
            break
        except Exception:
            print("[worker] loop error:\n", traceback.format_exc())
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main_loop()
