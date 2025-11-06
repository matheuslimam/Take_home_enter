# anchors_reading_span.py (LLM fallback + timers, sem desenho, com LLM-bulk sanitize/fill + JSON extractor final)
import os, json, math, unicodedata, time, statistics
import numpy as np
import regex as rx
import fitz  # PyMuPDF

# ------------- LLM (opcional) -------------
from dotenv import load_dotenv, find_dotenv
_ = load_dotenv(find_dotenv(usecwd=True)) or load_dotenv(os.path.join(os.getcwd(), ".env")) \
    or load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

ENABLE_LLM_FALLBACK = True
LLM_MODEL = "gpt-5-mini"
LLM_MAX_OUTPUT_TOKENS = 80
LLM_REASONING_EFFORT = "minimal"  # minimal|low|medium|high
LLM_TEXT_VERBOSITY = "low"        # low|medium|high
LLM_SANITIZE_EXISTING = True      # se True, sobrescreve com versão sanitizada mesmo quando já havia valor
# LLM só em casos compostos ou quando há âncora do schema mas faltou texto
LLM_ONLY_MISSING_OR_COMPOSED = True


_openai_client_cached = None
def _get_openai_client():
    global _openai_client_cached
    if _openai_client_cached is not None:
        return _openai_client_cached
    try:
        try:
            from openai import OpenAI
        except Exception as e:
            print(f"[LLM] import openai falhou: {e}")
            return None
        api_key = (os.environ.get("OPENAI_API_KEY") or
                   os.environ.get("OPENAI_APIKEY") or
                   os.environ.get("OPENAI_KEY"))
        if not api_key:
            print("[LLM] API key não encontrada nas env vars (OPENAI_API_KEY / OPENAI_APIKEY / OPENAI_KEY).")
            return None
        _openai_client_cached = OpenAI(api_key=api_key)
        return _openai_client_cached
    except Exception as e:
        print(f"[LLM] erro criando cliente: {e}")
        return None

def _responses_create_safe(**kwargs):
    try:
        client = _get_openai_client()
        if not client:
            return None, "no_client"
        resp = client.responses.create(**kwargs)
        return resp, None
    except Exception as e:
        return None, str(e)

LLM_STATS = {"attempts": 0, "success": 0}

# ---------------- helpers de texto ----------------
import unicodedata as _ud
def remove_accents(s: str) -> str:
    return "".join(ch for ch in _ud.normalize("NFD", s) if _ud.category(ch) != "Mn")

def norm_txt(s: str) -> str:
    return rx.sub(r"\s+", " ", remove_accents(str(s)).lower()).strip()

def camel_to_words(s: str) -> str:
    s = rx.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ")
    return rx.sub(r"\s+", " ", s).strip()

def consonants_only(s: str) -> str:
    return rx.sub(r"[aeiouAEIOU]", "", s)

def prefix_cut(s: str, n: int = 4) -> str:
    return s[:max(1, n)]

def sanitize_value_text(text: str) -> str:
    s = rx.sub(r"^\s*[:\-–—]\s*", "", text.strip())
    if rx.match(r"^[A-Za-zÀ-ÿ0-9 ]{1,30}\s*:\s*", s):
        s = rx.sub(r"^[^:]*:\s*", "", s)
    if rx.match(r"^[A-Za-zÀ-ÿ0-9 ]{1,30}\s*:\s*", s):
        s = rx.sub(r"^[^:]*:\s*", "", s)
    return s.strip()

def page_text_from_words(words_xy, max_chars=2000):
    def yc(w): return 0.5*(w[1]+w[3])
    def xc(w): return 0.5*(w[0]+w[2])
    seq = sorted(words_xy, key=lambda w: (yc(w), xc(w)))
    text = " ".join(str(w[4]) for w in seq if str(w[4]).strip())
    if len(text) > max_chars:
        text = text[:max_chars]
    return text

def _schema_keys_null(schema: dict) -> dict:
    """
    Recebe um schema possivelmente com descrições (strings) nos valores
    e retorna um novo dict apenas com as chaves e valores = None.
    """
    if not isinstance(schema, dict):
        return {}
    return {k: None for k in schema.keys()}

# ---------------- LLM por campo (fallback) ----------------
def llm_extract_value(key: str, context: str):
    try:
        LLM_STATS["attempts"] += 1
    except Exception:
        pass

    if not ENABLE_LLM_FALLBACK:
        print(f"[LLM] skip (disabled) key={key!r}")
        return None
    if not context or not str(context).strip():
        print(f"[LLM] skip (empty_context) key={key!r}")
        return None

    ctx = str(context).strip()
    if len(ctx) > 320:
        ctx = ctx[:320]

    k = (key or "").lower()
    RX_PHONE = rx.compile(r"\b(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}\b")
    RX_NUM   = rx.compile(r"\b\d{3,6}\b")
    RX_CPF   = rx.compile(r"\b\d{3}\.?(\d{3}\.){1}\d{3}-?\d{2}\b")
    RX_DATE  = rx.compile(r"\b([0-3]?\d)[/.-]([01]?\d)[/.-]([12]\d{3})\b")

    def _first(rx_pat):
        m = rx_pat.search(ctx)
        return m.group(0) if m else None

    if "telefone" in k:
        hit = _first(RX_PHONE)
        if hit:
            print(f"[LLM] fast-path telefone key={key!r} -> {hit}")
            try: LLM_STATS["success"] += 1
            except Exception: pass
            return hit

    if any(t in k for t in ("inscricao", "n_registro", "registro", "oab")):
        hit = _first(RX_NUM)
        if hit:
            print(f"[LLM] fast-path num key={key!r} -> {hit}")
            try: LLM_STATS["success"] += 1
            except Exception: pass
            return hit

    if "cpf" in k:
        hit = _first(RX_CPF)
        if hit:
            print(f"[LLM] fast-path cpf key={key!r} -> {hit}")
            try: LLM_STATS["success"] += 1
            except Exception: pass
            return hit

    if "data" in k:
        hit = _first(RX_DATE)
        if hit:
            print(f"[LLM] fast-path data key={key!r} -> {hit}")
            try: LLM_STATS["success"] += 1
            except Exception: pass
            return hit

    start_t = time.perf_counter()
    client = _get_openai_client()
    if not client:
        print(f"[LLM] skip (no_client) key={key!r}")
        return None

    system_msg = (
        "Você é um extrator. Dado um trecho de OCR possivelmente ruidoso, "
        "retorne SOMENTE o valor do campo especificado, sem comentários. "
        "Se achar a informação no trecho, responda com o valor exato. "
        "Se não existir no trecho, responda exatamente: null."
    )
    user_msg = f"Campo: {key}\nTrecho:\n{ctx}\nResponda apenas o valor, ou null."

    payload = dict(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_msg}]},
            {"role": "user",   "content": [{"type": "input_text", "text": user_msg}]},
        ],
        reasoning={"effort": LLM_REASONING_EFFORT},
        text={"verbosity": LLM_TEXT_VERBOSITY},
        max_output_tokens=max(8, int(LLM_MAX_OUTPUT_TOKENS or 24)),
    )

    try:
        if "_responses_create_safe" in globals():
            resp, err = _responses_create_safe(**payload)
            if err or not resp:
                dur = time.perf_counter() - start_t
                print(f"[LLM] error key={key!r} took={dur:.2f}s err={err or 'unknown'}")
                return None
        else:
            resp = client.responses.create(**payload)
    except Exception as e:
        dur = time.perf_counter() - start_t
        print(f"[LLM] exception key={key!r} took={dur:.2f}s err={e}")
        return None

    out = getattr(resp, "output_text", None)
    if not out:
        try:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        parts.append(getattr(c, "text", "") or "")
            out = "\n".join(p for p in parts if p).strip() or None
        except Exception:
            out = None

    if not out:
        dur = time.perf_counter() - start_t
        print(f"[LLM] empty_output key={key!r} took={dur:.2f}s")
        return None

    val = out.strip().splitlines()[0].strip(" \t'\"")
    if val.lower() == "null" or val == "":
        dur = time.perf_counter() - start_t
        print(f"[LLM] no_value key={key!r} took={dur:.2f}s -> null")
        return None

    dur = time.perf_counter() - start_t
    try:
        LLM_STATS["success"] += 1
    except Exception:
        pass
    shown = (val[:80] + "…") if len(val) > 80 else val
    print(f"[LLM] key={key!r} took={dur:.2f}s -> {shown}")
    return val

# -------- LLM em lote (página): preencher + sanitizar --------
def llm_sanitize_and_fill_bulk(keys, page_text, current_values):
    if not ENABLE_LLM_FALLBACK:
        return [ (current_values.get(k) or "").strip() or "null" for k in keys ]
    client = _get_openai_client()
    if not client:
        print("[LLM-BULK] skip (no_client)")
        return [ (current_values.get(k) or "").strip() or "null" for k in keys ]

    kv_lines = [f"{k}={(current_values.get(k) or '').strip()}" for k in keys]

    system_msg = (
        "Você é um extrator/sanitizador. Dado um TEXTO OCR e uma LISTA ordenada de chaves com valores brutos, "
        "retorne, NA MESMA ORDEM DAS CHAVES, apenas os valores finais, separados por ponto e vírgula. "
        "Regras:\n"
        "- Se um valor bruto existir mas estiver sujo, normalize-o (datas dd/mm/aaaa; telefones com DDD; remova prefixos 'rótulo:' etc.).\n"
        "- Se não houver valor no texto, escreva exatamente: null.\n"
        "- Não invente valores. Não acrescente comentários. Apenas a lista de valores, separada por ';'."
    )
    user_msg = (
        "TEXTO_OCR:\n"
        f"{page_text}\n\n"
        "CHAVES_E_VALORES_BRUTOS (na ordem):\n" + "\n".join(kv_lines) + "\n\n"
        "Formato de resposta (apenas esta linha, sem espaços extras):\n"
        "valor1;valor2;valor3;...\n"
        "Use 'null' quando o valor não existir."
    )

    payload = dict(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_msg}]},
            {"role": "user",   "content": [{"type": "input_text", "text": user_msg}]},
        ],
        reasoning={"effort": LLM_REASONING_EFFORT},
        text={"verbosity": LLM_TEXT_VERBOSITY},
        max_output_tokens=max(64, 8*len(keys))
    )

    t0 = time.perf_counter()
    try: LLM_STATS["attempts"] += 1
    except Exception: pass

    try:
        resp, err = _responses_create_safe(**payload)
        if err or not resp:
            dur = time.perf_counter() - t0
            print(f"[LLM-BULK] error took={dur:.2f}s err={err or 'unknown'}")
            return [ (current_values.get(k) or "").strip() or "null" for k in keys ]
    except Exception as e:
        dur = time.perf_counter() - t0
        print(f"[LLM-BULK] exception took={dur:.2f}s err={e}")
        return [ (current_values.get(k) or "").strip() or "null" for k in keys ]

    out = getattr(resp, "output_text", None)
    if not out:
        try:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        parts.append(getattr(c, "text", "") or "")
            out = "\n".join(p for p in parts if p).strip() or None
        except Exception:
            out = None

    dur = time.perf_counter() - t0
    if not out:
        print(f"[LLM-BULK] empty_output took={dur:.2f}s")
        return [ (current_values.get(k) or "").strip() or "null" for k in keys ]

    line = out.strip().splitlines()[0].strip()
    vals = [v.strip(" \t'\"") for v in line.split(";")]

    if len(vals) < len(keys):   vals += ["null"] * (len(keys) - len(vals))
    elif len(vals) > len(keys): vals = vals[:len(keys)]

    changed_ok = any((vals[i] or "").lower() != "null" and (vals[i] != (current_values.get(k) or "").strip()) for i, k in enumerate(keys))
    if changed_ok:
        try: LLM_STATS["success"] += 1
        except Exception: pass

    preview = ";".join(vals)[:100]
    print(f"[LLM-BULK] took={dur:.2f}s -> {preview}{'…' if len(preview)==100 else ''}")
    return vals

# -------- NOVO: LLM final por SCHEMA (JSON extractor) --------
def _strip_to_json(text: str) -> str:
    """tenta isolar um objeto JSON do output (remove fences/ruídos)."""
    if not text:
        return ""
    s = text.strip()
    # remove fences ```json ... ```
    s = rx.sub(r"^```(?:json)?\s*", "", s, flags=rx.IGNORECASE)
    s = rx.sub(r"\s*```$", "", s)
    # tenta recortar do primeiro '{' ao último '}'
    m0 = s.find("{")
    m1 = s.rfind("}")
    if 0 <= m0 < m1:
        return s[m0:m1+1]
    return s

def llm_extract_schema_json(full_text: str, missing_schema: dict) -> dict:
    """
    Recebe o TEXTO completo do documento e um SCHEMA parcial (apenas os campos faltantes).
    Pede à LLM para responder SOMENTE com o JSON no formato do schema.
    Retorna um dict (pode conter valores 'null' para não encontrados).
    """
    if not ENABLE_LLM_FALLBACK or not missing_schema:
        return {}
    client = _get_openai_client()
    if not client:
        print("[LLM-JSON] skip (no_client)")
        return {}

    # compacta texto para evitar tokens demais (mantém começo e fim)
    MAX_TXT = 7000
    if len(full_text) > MAX_TXT:
        head = full_text[:MAX_TXT//2]
        tail = full_text[-MAX_TXT//2:]
        full_text = head + "\n...\n" + tail

    prompt = (
        "Você é um assistente de extração de dados (JSON extractor).\n"
        "Extraia as informações solicitadas do texto de um documento PDF.\n"
        "O texto pode estar desordenado.\n\n"
        "TEXTO DO DOCUMENTO:\n"
        "---\n"
        f"{full_text}\n"
        "---\n\n"
        "SCHEMA JSON PARA EXTRAÇÃO:\n"
        "(Responda *apenas* com o JSON. Se um campo não for encontrado, use 'null'.)\n\n"
        f"{json.dumps(missing_schema, indent=2, ensure_ascii=False)}"
    )

    payload = dict(
        model=LLM_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Responda apenas com JSON válido conforme o schema fornecido. Sem comentários."}]},
            {"role": "user",   "content": [{"type": "input_text", "text": prompt}]},
        ],
        reasoning={"effort": LLM_REASONING_EFFORT},
        text={"verbosity": LLM_TEXT_VERBOSITY},
        max_output_tokens=max(128, 16*max(1, len(missing_schema)))
    )

    t0 = time.perf_counter()
    try: LLM_STATS["attempts"] += 1
    except Exception: pass

    try:
        resp, err = _responses_create_safe(**payload)
        if err or not resp:
            dur = time.perf_counter() - t0
            print(f"[LLM-JSON] error took={dur:.2f}s err={err or 'unknown'}")
            return {}
    except Exception as e:
        dur = time.perf_counter() - t0
        print(f"[LLM-JSON] exception took={dur:.2f}s err={e}")
        return {}

    out = getattr(resp, "output_text", None)
    if not out:
        try:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        parts.append(getattr(c, "text", "") or "")
            out = "\n".join(p for p in parts if p).strip() or None
        except Exception:
            out = None

    dur = time.perf_counter() - t0
    if not out:
        print(f"[LLM-JSON] empty_output took={dur:.2f}s")
        return {}

    js = _strip_to_json(out)
    try:
        obj = json.loads(js)
        try: LLM_STATS["success"] += 1
        except Exception: pass
        preview = json.dumps(obj, ensure_ascii=False)[:120]
        print(f"[LLM-JSON] took={dur:.2f}s -> {preview}{'…' if len(preview)==120 else ''}")
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        print(f"[LLM-JSON] invalid_json err={e}")
        return {}

# ---------------- etiquetas/âncoras e leitura ----------------
def label_variants(key_name: str) -> list[str]:
    base_words = camel_to_words(key_name)
    base = norm_txt(base_words)
    parts = base.split()
    V = set()
    V.add(base); V.add("".join(parts))
    no_v = [consonants_only(p) for p in parts]
    V.add(" ".join(no_v)); V.add("".join(no_v))
    pref = [prefix_cut(p, 4) for p in parts]
    V.add(" ".join(pref)); V.add("".join(pref))
    V.add(" ".join(p + "." for p in pref)); V.add("".join(p + "." for p in pref))
    if parts:
        V.add(" ".join(p + "." for p in parts)); V.add("".join(p + "." for p in parts))
        V.add(parts[0]); V.add(prefix_cut(parts[0], 3)); V.add(prefix_cut(consonants_only(parts[0]), 3))
        V.add(parts[0] + "."); V.add(prefix_cut(parts[0], 3) + ".")
    return sorted({rx.sub(r"\.+$", ".", rx.sub(r"\s+", " ", v).strip()) for v in V},
                  key=len, reverse=True)

def find_anchor_by_label(words, label_text: str):
    def nrm(s: str) -> str:
        s = "".join(ch for ch in _ud.normalize("NFD", s) if _ud.category(ch) != "Mn")
        s = rx.sub(r"[\p{P}\p{S}]+", " ", s)
        s = rx.sub(r"\s+", " ", s).strip().lower()
        return s

    norm_tokens = [nrm(w[4]) for w in words]
    clean = [(i, t) for i, t in enumerate(norm_tokens) if t]
    if not clean:
        return None

    base = nrm(label_text.replace("_", " "))
    parts = [p for p in base.split() if p]
    variants = {base, "".join(parts)}
    abbr = " ".join(p[:4] + "." for p in parts)
    variants.add(nrm(abbr)); variants.add(nrm(abbr.replace(".", "")))

    MAX_W = 8
    best = None
    for wlen in range(min(MAX_W, len(clean)), 0, -1):
        for s in range(0, len(clean) - wlen + 1):
            win = clean[s:s + wlen]
            joined_spc = " ".join(t for _, t in win)
            joined_nos = "".join(t for _, t in win)
            is_exact = (joined_spc in variants) or (joined_nos in variants)
            contains  = any(v in joined_spc or v in joined_nos for v in variants)
            if not (is_exact or contains):
                continue
            score = (1 if is_exact else 0, wlen, max(len(joined_spc), len(joined_nos)))
            if (best is None) or (score > best[:3]):
                best = (*score, win[0][0], win[-1][0])

    if best is None:
        return None

    _, _, _, i0, i1 = best
    xs, ys, span = [], [], []
    xs0, ys0, xs1, ys1 = [], [], [], []
    for k in range(i0, i1 + 1):
        x0, y0, x1, y1, _ = words[k]
        xs.append(0.5*(x0+x1)); ys.append(0.5*(y0+y1)); span.append(k)
        xs0.append(x0); ys0.append(y0); xs1.append(x1); ys1.append(y1)
    ax = float(np.mean(xs)); ay = float(np.mean(ys))
    span_bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
    return (ax, ay, span, span_bbox)

def is_abbrev_token(t: str) -> bool:
    return bool(rx.match(r"^([A-Za-zÀ-ÿ]{1,4}\.)+$", t.strip()))

def looks_like_label(words_seq_text: str) -> bool:
    s = words_seq_text.strip()
    if not s: return False
    if len(s) > 30: return False
    if rx.search(r"\d{3,}", s): return False
    if s.endswith(":"): return True
    if any(is_abbrev_token(tok) for tok in s.split()): return True
    toks = s.split()
    if 1 <= len(toks) <= 3 and sum(tok[:1].isupper() for tok in toks) >= 1:
        return True
    return False

def label_score(label_text: str, right_has_value: bool, down_has_value: bool, bold_like: bool) -> int:
    s = label_text.strip()
    score = 0
    if s.endswith(":"): score += 1
    if any(is_abbrev_token(tok) for tok in s.split()): score += 1
    if right_has_value: score += 1
    if down_has_value: score += 1
    if bold_like: score += 1
    if 2 <= len(s) <= 10: score += 1
    return score

def extract_word_spans(page):
    D = page.get_text("dict")
    out = []
    for block in D.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "") or ""
                words = [w for w in rx.split(r"(\s+)", txt) if w and not w.isspace()]
                if not words:
                    continue
                x0,y0,x1,y1 = span["bbox"]
                W = x1 - x0
                step = max(1.0, W / max(1, len(words)))
                cur = x0
                is_bold = bool(span.get("flags", 0) & 2) or "Bold" in (span.get("font", "") or "")
                for w in words:
                    w0 = cur; w1 = cur + step
                    out.append((float(w0), float(y0), float(w1), float(y1), w, is_bold))
                    cur = w1
    if not out:
        base_words = page.get_text("words")
        out = [(w[0],w[1],w[2],w[3],w[4],False) for w in base_words]
    return out

def find_generic_anchors(page, Y_BAND=18.0, RADIUS=220.0, GUTTER_PAD_X=10.0, GUTTER_W_MIN=60.0, GUTTER_W_FACTOR=0.6):
    words_attr = extract_word_spans(page)
    def ycenter(i):
        x0,y0,x1,y1,_,_ = words_attr[i]
        return 0.5*(y0+y1)
    def same_line(i,j):
        return abs(ycenter(i)-ycenter(j)) <= Y_BAND

    anchors = []
    N = len(words_attr)
    centers = [((w[0]+w[2])*0.5, (w[1]+w[3])*0.5) for w in words_attr]

    for i in range(N):
        for L in range(1,5):
            j = i + L - 1
            if j >= N: break
            if not same_line(i, j): break
            seq = words_attr[i:j+1]
            txt = " ".join(t for *_, t, _ in seq).strip()
            if not looks_like_label(txt):
                continue

            xs0 = [s[0] for s in seq]; ys0 = [s[1] for s in seq]
            xs1 = [s[2] for s in seq]; ys1 = [s[3] for s in seq]
            x0,y0,x1,y1 = min(xs0), min(ys0), max(xs1), max(ys1)
            ax = 0.5*(x0+x1); ay = 0.5*(y0+y1)
            lbl_w = max(1.0, x1 - x0)
            gx0 = x0 - 10.0
            gx1 = x0 + max(60.0, lbl_w * 0.60)

            best_r = None
            for k in range(N):
                if i <= k <= j: continue
                wx, wy = centers[k]
                dx, dy = wx - ax, wy - ay
                if dx <= 0 or abs(dy) > Y_BAND: continue
                dist = math.hypot(dx, dy)
                if dist > RADIUS: continue
                if best_r is None or dx < best_r[1] - 1e-9:
                    best_r = (k, dx, dist)
            right_has = bool(best_r and rx.search(r"[A-Za-zÀ-ÿ0-9]", words_attr[best_r[0]][4]))

            best_d = None
            for k in range(N):
                if i <= k <= j: continue
                wx, wy = centers[k]
                dy = wy - ay
                if dy <= 0: continue
                cx = wx
                if not (gx0 <= cx <= gx1): continue
                if best_d is None or dy < best_d[1] - 1e-9:
                    best_d = (k, dy)
            down_has = bool(best_d and rx.search(r"[A-Za-zÀ-ÿ0-9]", words_attr[best_d[0]][4]))

            bold_like = any(s[-1] for s in seq)
            score = label_score(txt, right_has, down_has, bold_like)
            if score >= 2:
                anchors.append({
                    "key": txt.rstrip(" :"),
                    "anchor": (ax, ay),
                    "label_span": set(range(i, j+1)),
                    "label_bbox": (x0,y0,x1,y1),
                    "gutter": (gx0,gx1),
                    "score": score,
                    "origin": "generic"
                })

    anchors.sort(key=lambda a: (-a["score"], a["anchor"][1], a["anchor"][0]))
    kept = []
    taken_pts = []
    for a in anchors:
        ax, ay = a["anchor"]
        if not taken_pts:
            kept.append(a); taken_pts.append((ax,ay)); continue
        if min(math.hypot(ax-x, ay-y) for (x,y) in taken_pts) >= 10.0:
            kept.append(a); taken_pts.append((ax,ay))
    return kept, words_attr

def bbox_iou(b1, b2):
    x0 = max(b1[0], b2[0]); y0 = max(b1[1], b2[1])
    x1 = min(b1[2], b2[2]); y1 = min(b1[3], b2[3])
    iw = max(0.0, x1 - x0); ih = max(0.0, y1 - y0)
    inter = iw * ih
    a1 = max(0.0, (b1[2]-b1[0])) * max(0.0, (b1[3]-b1[1]))
    a2 = max(0.0, (b2[2]-b2[0])) * max(0.0, (b2[3]-b2[1]))
    union = a1 + a2 - inter + 1e-6
    return inter / union

def repel_anchors_global(anchors):
    def pri(a):
        base = 1 if str(a.get("origin","")).startswith("schema") else 0
        return (base, a.get("score", 0.0))
    anchors_sorted = sorted(anchors, key=lambda a: (-pri(a)[0], -pri(a)[1], a["anchor"][1], a["anchor"][0]))
    kept = []
    for a in anchors_sorted:
        ok = True
        ax, ay = a["anchor"]; bb = a["label_bbox"]
        for b in kept:
            bx, by = b["anchor"]; bb2 = b["label_bbox"]
            if math.hypot(ax-bx, ay-by) < 10.0 - 1e-6: ok = False; break
            if bbox_iou(bb, bb2) > 0.10: ok = False; break
        if ok: kept.append(a)
    return kept

def calibrate_layout(words_xy):
    h = np.array([w[3]-w[1] for w in words_xy], dtype=float)
    h_med = float(np.median(h)) if len(h) else 16.0
    return {
        "Y_BAND":   max(10.0, 0.65*h_med),
        "GAP_MAX":  max(14.0, 1.30*h_med),
        "LINE_JUMP":max(18.0, 1.75*h_med),
        "RADIUS":   max(180.0, 7.5*h_med),
    }

def nearest_right(ax, ay, centers, allowed, label_bbox, y_band, r_right):
    cut_x = label_bbox[2] + 2.0
    best = None
    for j in allowed:
        wx, wy = centers[j]
        if wx <= cut_x or abs(wy - ay) > y_band: continue
        dx = wx - ax
        dist = math.hypot(dx, wy - ay)
        if dist > r_right: continue
        cand = (j, dx, dist)
        if best is None or dx < best[1] - 1e-9 or (math.isclose(dx, best[1]) and dist < best[2]-1e-9):
            best = cand
    return best

def nearest_down(ax, ay, centers, allowed, gutter, y_band, r_down):
    gx0, gx1 = gutter; gxc = 0.5*(gx0+gx1)
    best = None
    for j in allowed:
        wx, wy = centers[j]
        dy = wy - ay
        if dy <= 0 or dy > 5*y_band: continue
        cx = wx
        if not (gx0 - 14.0 <= cx <= gx1 + 14.0): continue
        dist = math.hypot(wx - ax, wy - ay)
        if dist > r_down: continue
        xdev = abs(cx - gxc)
        cost = dy + 0.5 * xdev
        if best is None or cost < best[1] - 1e-9 or (math.isclose(cost, best[1]) and dy < best[2]-1e-9):
            best = (j, cost, dy, xdev)
    return (best[0], best[2], best[1]) if best else None

def bbox_intersects(bb, cc, pad=2.0):
    x0 = max(bb[0]-pad, cc[0]-pad); y0 = max(bb[1]-pad, cc[1]-pad)
    x1 = min(bb[2]+pad, cc[2]+pad); y1 = min(bb[3]+pad, cc[3]+pad)
    return (x1 - x0) > 0 and (y1 - y0) > 0

def looks_like_heading(token_text, h_token, h_med):
    if not token_text or rx.search(r"\d", token_text): return False
    caps = sum(ch.isupper() for ch in token_text if ch.isalpha())
    letters = sum(ch.isalpha() for ch in token_text)
    caps_ratio = (caps / max(1, letters))
    return caps_ratio > 0.85 and h_token > 1.25*h_med

def reading_span_from_seed(words, centers, seed_idx, anchor_xy, gutter,
                           blockers=None, cfg=None):
    ax, ay = anchor_xy
    gx0, gx1 = gutter

    YB = (cfg or {}).get("Y_BAND", 18.0)
    GAP = (cfg or {}).get("GAP_MAX", 36.0)
    LJ  = (cfg or {}).get("LINE_JUMP", 32.0)

    W = [{"i": i, "x0": w[0], "y0": w[1], "x1": w[2], "y1": w[3], "t": w[4]}
         for i, w in enumerate(words)]

    def y_center(k): return 0.5*(W[k]["y0"]+W[k]["y1"])
    def x_center(k): return 0.5*(W[k]["x0"]+W[k]["x1"])
    def same_line(i, j): return abs(y_center(i) - y_center(j)) <= YB

    used = [seed_idx]
    tokens_total = 1
    lines_used = 1

    def bbox_of(idxs):
        xs0 = [W[i]["x0"] for i in idxs]; ys0 = [W[i]["y0"] for i in idxs]
        xs1 = [W[i]["x1"] for i in idxs]; ys1 = [W[i]["y1"] for i in idxs]
        return (min(xs0), min(ys0), max(xs1), max(ys1))

    def exceeds_geom(new_idxs):
        x0,y0,x1,y1 = bbox_of(new_idxs)
        w = x1 - x0; h = y1 - y0
        if 420.0 and w > 420.0:  return True
        if 140   and h > 140:    return True
        return False

    def ok_token(k):
        if not blockers: return True
        bbk = (W[k]["x0"], W[k]["y0"], W[k]["x1"], W[k]["y1"])
        return not any(bbox_intersects(bbk, bb) for bb in blockers)

    cur = seed_idx
    right_count_this_line = 0
    while True:
        cands = []
        for k in range(len(W)):
            if k in used or not ok_token(k) or not same_line(cur, k):
                continue
            if W[k]["x0"] <= W[cur]["x1"]: continue
            gap = W[k]["x0"] - W[cur]["x1"]
            if gap <= GAP: cands.append(k)
        if not cands: break
        if 8 and right_count_this_line >= 8: break
        if 20 and right_count_this_line >= 20: break
        if True and rx.search(r"(?:[.;:]\s*$)", W[cur]["t"] or ""): break

        next_k = min(cands, key=lambda k: (W[k]["x0"] - W[cur]["x1"], W[k]["x0"]))
        cand_used = used + [next_k]
        if 40 and (tokens_total + 1) > 40: break
        if exceeds_geom(cand_used): break
        used.append(next_k); cur = next_k
        tokens_total += 1
        right_count_this_line += 1

    h_med = np.median([W[i]["y1"]-W[i]["y0"] for i in range(len(W))]) if W else 16.0
    while True:
        last = max(used, key=lambda k: y_center(k))
        last_y = y_center(last)
        cands = []
        for k in range(len(W)):
            if k in used: continue
            yk = y_center(k)
            if yk <= last_y or (yk - last_y) > 32.0: continue
            cxk = x_center(k)
            if gx0 <= cxk <= gx1 and ok_token(k):
                cands.append(k)
        if not cands: break
        if 3 and lines_used >= 1 + 3: break
        start_k = min(cands, key=lambda k: (abs(x_center(k) - 0.5*(gx0+gx1)), y_center(k)))
        if looks_like_heading(W[start_k]["t"], W[start_k]["y1"]-W[start_k]["y0"], h_med): break
        if 40 and (tokens_total + 1) > 40: break
        if exceeds_geom(used + [start_k]): break
        used.append(start_k); tokens_total += 1; lines_used += 1

        cur = start_k
        right_count_this_line = 0
        while True:
            candsR = []
            for k in range(len(W)):
                if k in used or not ok_token(k) or not same_line(cur, k): continue
                if W[k]["x0"] <= W[cur]["x1"]: continue
                gap = W[k]["x0"] - W[cur]["x1"]
                if gap <= 36.0: candsR.append(k)
            if not candsR: break
            if 8 and right_count_this_line >= 8: break
            if 20 and right_count_this_line >= 20: break
            if True and rx.search(r"(?:[.;:]\s*$)", W[cur]["t"] or ""): break
            next_k = min(candsR, key=lambda k: (W[k]["x0"] - W[cur]["x1"], W[k]["x0"]))
            cand_used = used + [next_k]
            if 40 and (tokens_total + 1) > 40: break
            if exceeds_geom(cand_used): break
            used.append(next_k); cur = next_k; tokens_total += 1; right_count_this_line += 1
        if len(used) > 300: break

    used_sorted = sorted(set(used), key=lambda k: (0.5*(W[k]["y0"]+W[k]["y1"]), 0.5*(W[k]["x0"]+W[k]["x1"])))
    xs0 = [W[i]["x0"] for i in used_sorted]; ys0 = [W[i]["y0"] for i in used_sorted]
    xs1 = [W[i]["x1"] for i in used_sorted]; ys1 = [W[i]["y1"] for i in used_sorted]
    bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
    text = " ".join(W[i]["t"] for i in used_sorted)
    return used_sorted, bbox, sanitize_value_text(text)

def local_llm_context(words_xy, seed_idx, label_bbox, gutter, ay, y_band):
    if not words_xy:
        return ""
    x0_lbl, _, x1_lbl, _ = label_bbox
    gx0, gx1 = gutter
    gutter_w = max(1.0, gx1 - gx0)
    wx0 = min(x1_lbl, gx0) - 10.0
    wx1 = max(x1_lbl + 1.5*gutter_w, gx1 + 30.0)
    wy0 = ay - 2.0*y_band
    wy1 = ay + 5.0*y_band

    def y_center(w): return 0.5*(w[1]+w[3])
    def x_center(w): return 0.5*(w[0]+w[2])

    cand = []
    for w in words_xy:
        cx, cy = x_center(w), y_center(w)
        if wx0 <= cx <= wx1 and wy0 <= cy <= wy1:
            cand.append(w)
    cand_sorted = sorted(cand, key=lambda w: (y_center(w), x_center(w)))
    text = " ".join(str(w[4]) for w in cand_sorted if str(w[4]).strip())
    if len(text) > 600:
        text = text[:600]
    return text

def process_page(doc, page, words, anchor_names):
    words_xy = [(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])) for w in words]
    cfg = calibrate_layout(words_xy)
    local_YB, local_GAP, local_LJ, local_RAD = cfg["Y_BAND"], cfg["GAP_MAX"], cfg["LINE_JUMP"], cfg["RADIUS"]

    anchors = []
    missing = []
    for key in anchor_names:
        hit = find_anchor_by_label(words_xy, key)
        if hit:
            ax, ay, span, bbox = hit
            x0, y0, x1, y1 = bbox
            lbl_w = max(1.0, x1 - x0)
            gx0 = x0 - 10.0
            gx1 = x0 + max(60.0, lbl_w * 0.60)
            anchors.append({
                "key": key, "anchor": (ax, ay), "label_span": set(span),
                "label_bbox": bbox, "gutter": (gx0, gx1), "origin": "schema", "score": 10.0
            })
        else:
            missing.append(key)

    if True and missing:
        gen_anchors, _ = find_generic_anchors(page, Y_BAND=local_YB, RADIUS=local_RAD,
                                              GUTTER_PAD_X=10.0, GUTTER_W_MIN=60.0, GUTTER_W_FACTOR=0.60)
        for key in missing:
            # escolha fuzzy
            best = None
            for g in gen_anchors:
                # fuzzy simples
                A = norm_txt(key); B = norm_txt(g["key"])
                tokens = set(rx.findall(r"[a-z0-9]+", A)) & set(rx.findall(r"[a-z0-9]+", B))
                sim = len(tokens) / max(1, len(set(rx.findall(r"[a-z0-9]+", A)) | set(rx.findall(r"[a-z0-9]+", B))))
                richness = 0.05 * (len((g["key"] or "").split()) - 1) + 0.01 * g.get("score", 0)
                sim_adj = sim + richness
                if best is None or sim_adj > best[0]:
                    best = (sim_adj, g)
            thr = 0.35 if len(gen_anchors) > 12 else 0.30
            if best and best[0] >= thr:
                g = best[1]
                anchors.append({
                    "key": key, "anchor": g["anchor"], "label_span": g["label_span"],
                    "label_bbox": g["label_bbox"], "gutter": g["gutter"],
                    "origin": f"generic:{g['key']}", "score": g.get("score", 2.0)
                })

    anchors = repel_anchors_global(anchors)

    centers = [((w[0]+w[2])*0.5, (w[1]+w[3])*0.5) for w in words_xy]
    all_excluded = set().union(*(a["label_span"] for a in anchors)) if anchors else set()

    results = []
    taken = set()
    for a in sorted(anchors, key=lambda r: (r["anchor"][1], r["anchor"][0])):
        ax, ay = a["anchor"]
        allowed = set(range(len(words_xy))) - all_excluded - taken

        r_right = 0.0 if 0.0 and 0.0 > 0 else local_RAD
        r_down  = 0.0 if 0.0 and 0.0 > 0 else local_RAD

        best_r = nearest_right(ax, ay, centers, allowed, a["label_bbox"], local_YB, r_right)
        best_d = nearest_down(ax, ay, centers, allowed, a["gutter"], local_YB, r_down)

        seed_idx = None; direction = None
        if best_r and best_d:
            if best_r[1] <= best_d[1] + 2.0: seed_idx = best_r[0]; direction = "right"
            else: seed_idx = best_d[0]; direction = "down"
        elif best_r: seed_idx = best_r[0]; direction = "right"
        elif best_d: seed_idx = best_d[0]; direction = "down"

        all_label_bboxes = [x["label_bbox"] for x in anchors]
        blockers = [bb for bb in all_label_bboxes if bb is not a["label_bbox"]]

        if seed_idx is None:
            # Só usa LLM se a âncora veio do schema (não âncora genérica inferida)
            llm_val = ""
            if (not LLM_ONLY_MISSING_OR_COMPOSED) or str(a.get("origin","")).startswith("schema"):
                ctx = local_llm_context(words_xy, None, a["label_bbox"], a["gutter"], ay, local_YB)
                if ctx:
                    llm_val = llm_extract_value(a["key"], ctx) or ""
            results.append({**a, "seed": None, "tokens": [], "bbox": None,
                            "text": llm_val, "composed": False, "dir": None})
            continue


        tokens, bbox, text = reading_span_from_seed(
            words_xy, centers, seed_idx, a["anchor"], a["gutter"],
            blockers=blockers,
            cfg={"Y_BAND": local_YB, "GAP_MAX": local_GAP, "LINE_JUMP": local_LJ}
        )

        if (text is None) or (str(text).strip() == ""):
            ctx = local_llm_context(words_xy, seed_idx, a["label_bbox"], a["gutter"], ay, local_YB)
            llm_val = llm_extract_value(a["key"], ctx) if ctx else None
            if llm_val is not None:
                text = llm_val

        taken.update(tokens)
        results.append({**a, "seed": seed_idx, "tokens": tokens, "bbox": bbox,
                        "text": text or "", "composed": len(tokens) > 1, "dir": direction})
    return anchors, results, words_xy

# ---------------- PATHS ----------------
BASE = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
PDF_DIR = os.path.normpath(os.path.join(BASE, "..", "Data", "pdfs"))
DATASET_PATH = os.path.normpath(os.path.join(BASE, "..", "dataset3.json"))

# ---------------- main ----------------
def main():
    if not os.path.isfile(DATASET_PATH):
        print(f"[ERR] dataset.json não encontrado: {DATASET_PATH}"); return
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        print("[ERR] dataset.json deve ser LISTA de itens."); return

    all_outputs = []
    for it in items:
        pdf_rel = it.get("pdf_path", "")
        schema = it.get("extraction_schema", {}) or {}
        anchor_names = list(schema.keys())
        pdf_path = os.path.join(PDF_DIR, pdf_rel)
        if not os.path.isfile(pdf_path):
            print(f"[WARN] PDF não encontrado: {pdf_rel}")
            all_outputs.append({"pdf": pdf_rel, "result": None, "error": "pdf_not_found"})
            continue

        print(f"[+] PDF: {pdf_rel} | campos: {anchor_names}")
        doc = fitz.open(pdf_path)

        # juntamos texto completo do doc para o passo final JSON extractor
        full_text_parts = []

        extracted = {k: None for k in anchor_names}
        page_times = []
        for pno in range(len(doc)):
            page = doc[pno]
            words = page.get_text("words")

            # acumula texto integral (limite por pagina ~ 3000 chars para não explodir)
            ptxt = (page.get_text("text") or "")
            if len(ptxt) > 3000:
                ptxt = ptxt[:2000] + "\n...\n" + ptxt[-1000:]
            full_text_parts.append(ptxt)

            t0 = time.perf_counter()
            anchors, results, words_xy = process_page(doc, page, words, anchor_names)
            t1 = time.perf_counter()
            elapsed = t1 - t0
            page_times.append(elapsed)
            print(f"  [tempo] página {pno+1}: {elapsed:.3f}s")

            # (1) aplicar engine de âncoras
            page_raw = {}
            for r in results:
                k = r["key"]
                val = (r.get("text") or "").strip()
                page_raw[k] = val
                if k in extracted and (extracted[k] is None or str(extracted[k]).strip() == ""):
                    if val:
                        extracted[k] = val

            # (2) LLM bulk por página (sanitiza + tenta preencher vazios)
            page_text = page_text_from_words(words_xy, max_chars=1800) or (page.get_text("text") or "")[:1800]
            bulk_vals = llm_sanitize_and_fill_bulk(anchor_names, page_text, page_raw)
            for i, k in enumerate(anchor_names):
                v_model = (bulk_vals[i] or "").strip()
                if v_model.lower() == "null": v_model = ""
                if v_model:
                    if extracted.get(k) is None or not str(extracted[k]).strip():
                        extracted[k] = v_model
                    elif LLM_SANITIZE_EXISTING:
                        extracted[k] = v_model

        doc.close()

        # (3) PASSO FINAL — JSON extractor no TEXTO COMPLETO para campos faltantes
        missing_keys = [
            k for k in anchor_names
            if not (extracted.get(k) or "").strip()
            or any(
            r.get("key") == k and r.get("composed", False)
            for r in results
            )
        ]
        #missing_keys = anchor_names[:]  # passa o schema inteiro p/ re-sanitizar tudo

        if missing_keys:
            missing_schema = _schema_keys_null({k: schema.get(k, None) for k in missing_keys})
            full_text = "\n\n".join(full_text_parts)
            json_filled = llm_extract_schema_json(full_text, missing_schema)
            # aplica somente se vier valor não-nulo
            for k in missing_keys:
                v = json_filled.get(k, None)
                if isinstance(v, str):
                    v = v.strip()
                if v is None or (isinstance(v, str) and v.lower() == "null") or v == "":
                    continue
                extracted[k] = str(v)

        mean_time = statistics.mean(page_times) if page_times else 0.0
        print(f"  [média] {mean_time:.3f}s por página")
        print(f"  [LLM] attempts={LLM_STATS['attempts']} success={LLM_STATS['success']}")

        result_json = {k: (extracted[k] if extracted[k] is not None else None) for k in anchor_names}
        all_outputs.append({
            "pdf": pdf_rel,
            "result": result_json,
            "timing": {
                "per_page_seconds": [round(t, 6) for t in page_times],
                "mean_seconds": round(mean_time, 6)
            }
        })

    print("\n=== JSON FINAL ===")
    print(json.dumps(all_outputs, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()


def process_pdf_to_json(pdf_bytes: bytes, schema: dict) -> dict:
    """
    Abre o PDF em memória, roda a pipeline:
      1) Para cada página: âncoras -> reading span -> LLM bulk sanitize/fill
      2) Passo final: LLM JSON extractor no TEXTO COMPLETO (re-sanitiza tudo)
    Retorna um dict com os campos do schema. Campos não encontrados = None/strings vazias.
    """
    if not schema or not isinstance(schema, dict):
        return {}

    # Carrega o PDF do stream
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    anchor_names = list(schema.keys())
    extracted = {k: None for k in anchor_names}
    full_text_parts = []
    page_times = []

    for pno in range(len(doc)):
        page = doc[pno]
        # guarda texto integral da página (cortado)
        ptxt = (page.get_text("text") or "")
        if len(ptxt) > 3000:
            ptxt = ptxt[:2000] + "\n...\n" + ptxt[-1000:]
        full_text_parts.append(ptxt)

        t0 = time.perf_counter()
        words = page.get_text("words")
        anchors, results, words_xy = process_page(doc, page, words, anchor_names)
        t1 = time.perf_counter()
        page_times.append(t1 - t0)

        # aplica engine
        page_raw = {}
        for r in results:
            k = r["key"]
            val = (r.get("text") or "").strip()
            page_raw[k] = val
            if k in extracted and (extracted[k] is None or str(extracted[k]).strip() == ""):
                if val:
                    extracted[k] = val

        # LLM bulk sanitiza e tenta preencher
        page_text = page_text_from_words(words_xy, max_chars=1800) or (page.get_text("text") or "")[:1800]
        bulk_vals = llm_sanitize_and_fill_bulk(anchor_names, page_text, page_raw)
        for i, k in enumerate(anchor_names):
            v_model = (bulk_vals[i] or "").strip()
            if v_model.lower() == "null":
                v_model = ""
            if v_model:
                if extracted.get(k) is None or not str(extracted[k]).strip():
                    extracted[k] = v_model
                elif True:  # LLM_SANITIZE_EXISTING
                    extracted[k] = v_model

    doc.close()

    # Passo final: re-sanitizar tudo com JSON extractor no texto completo
    full_text = "\n\n".join(full_text_parts)
    json_filled = llm_extract_schema_json(full_text, _schema_keys_null(schema))

    # aplica se vier valor não-nulo
    for k in anchor_names:
        v = json_filled.get(k, None) if isinstance(json_filled, dict) else None
        if isinstance(v, str):
            v = v.strip()
        if v is None or (isinstance(v, str) and v.lower() == "null") or v == "":
            # mantém o do engine se já existir
            continue
        extracted[k] = str(v)

    # normaliza None -> None real (não "null" string)
    final = {k: (extracted.get(k) if extracted.get(k) not in ("", "null") else None) for k in anchor_names}
    return final
