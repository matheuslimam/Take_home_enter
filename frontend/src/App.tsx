import { useEffect, useMemo, useRef, useState } from 'react';
import { supabase, BUCKET_DOCS, BUCKET_RESULTS } from './lib/supabase';

// ====== TIPOS ======
type Job = {
  id: string;
  status: string;
  total_count: number;
  done_count: number;
  error_count: number;
};

type JobItem = {
  id: string;
  job_id: string;
  file_name: string;
  file_path: string;
  status: string;
  duration_ms: number | null;
  result_path: string | null;
  error_message: string | null;
};

type LabeledSchema = {
  label: string;
  extraction_schema: Record<string, any>;
  pdf_path?: string;
};

type ParsedInput =
  | { mode: 'single'; schema: Record<string, any> }
  | { mode: 'labeled'; items: LabeledSchema[] };

type FileWithSchema = {
  file: File;
  label?: string;
  schema: Record<string, any>;
  matchedBy: 'filename' | 'order' | 'single';
};

// ====== ENV ======
const FLY_API_URL = import.meta.env.VITE_FLY_API_URL || 'https://take-home-enter.fly.dev';

// ====== HELPERS ======
const prettyMs = (ms?: number | null) => (!ms && ms !== 0 ? '—' : `${(ms / 1000).toFixed(2)}s`);

function basename(p?: string) {
  if (!p) return '';
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

function parseInput(text: string): ParsedInput | null {
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      const items: LabeledSchema[] = parsed
        .filter((x) => x && typeof x === 'object')
        .map((x) => ({
          label: String(x.label ?? ''),
          extraction_schema: (x.extraction_schema ?? {}) as Record<string, any>,
          pdf_path: x.pdf_path ? String(x.pdf_path) : undefined,
        }))
        .filter((x) => x.label && x.extraction_schema && typeof x.extraction_schema === 'object');
      if (items.length > 0) return { mode: 'labeled', items };
      return null;
    }
    if (parsed && typeof parsed === 'object') {
      return { mode: 'single', schema: parsed as Record<string, any> };
    }
  } catch {
    // inválido
  }
  return null;
}

// ====== APP ======
export default function App() {
  const [files, setFiles] = useState<File[]>([]);
  const [schemaText, setSchemaText] = useState<string>('{\n  "nome": null\n}');
  const [job, setJob] = useState<Job | null>(null);
  const [items, setItems] = useState<JobItem[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [mapping, setMapping] = useState<FileWithSchema[]>([]);
  const [combinedUrl, setCombinedUrl] = useState<string | null>(null);

  const [serverStatus, setServerStatus] = useState<'idle' | 'connecting' | 'ok' | 'error'>('idle');
  const [isDragging, setIsDragging] = useState(false);

  const spinnerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ====== SPINNER ======
  useEffect(() => {
    let id: number | undefined;
    const el = spinnerRef.current;
    if (!el) return;
    let a = 0;
    const tick = () => {
      a = (a + 6) % 360;
      el.style.transform = `rotate(${a}deg)`;
      id = requestAnimationFrame(tick);
    };
    if (isProcessing) id = requestAnimationFrame(tick);
    return () => { if (id !== undefined) cancelAnimationFrame(id); };
  }, [isProcessing]);

  // ====== MAPA FILE→SCHEMA ======
  useEffect(() => {
    const parsed = parseInput(schemaText);
    if (!parsed) {
      setMapping([]);
      return;
    }
    if (parsed.mode === 'single') {
      setMapping(files.map((f) => ({
        file: f, schema: parsed.schema, matchedBy: 'single',
      })));
      return;
    }
    const labels = parsed.items;
    const byName = new Map<string, LabeledSchema>();
    labels.forEach((it) => {
      const b = basename(it.pdf_path || '');
      if (b) byName.set(b.toLowerCase(), it);
    });

    const next: FileWithSchema[] = [];
    const leftovers = labels.filter((it) => !it.pdf_path);
    const unused = new Set(leftovers.map((_, i) => i));

    files.forEach((f) => {
      const hit = byName.get(f.name.toLowerCase());
      if (hit) {
        next.push({ file: f, schema: hit.extraction_schema, label: hit.label, matchedBy: 'filename' });
      } else {
        let idx: number | undefined = undefined;
        for (const i of unused) { idx = i; break; }
        if (idx !== undefined) {
          const it = leftovers[idx];
          unused.delete(idx);
          next.push({ file: f, schema: it.extraction_schema, label: it.label, matchedBy: 'order' });
        } else if (labels.length > 0) {
          next.push({ file: f, schema: labels[0].extraction_schema, label: labels[0].label, matchedBy: 'order' });
        } else {
          next.push({ file: f, schema: {}, matchedBy: 'order' });
        }
      }
    });
    setMapping(next);
  }, [files, schemaText]);

  const parsedOk = useMemo(() => !!parseInput(schemaText), [schemaText]);
  const canStart = useMemo(() => files.length > 0 && parsedOk, [files, parsedOk]);
  const percent = job && job.total_count ? Math.min(100, Math.round((job.done_count / job.total_count) * 100)) : 0;

  // ====== SERVER WARM / HEALTH ======
  async function pingServer() {
    try {
      setServerStatus('connecting');
      const resp = await fetch(`${FLY_API_URL}/healthz`, { method: 'GET', mode: 'cors' });
      if (resp.ok) { setServerStatus('ok'); return true; }
      setServerStatus('error');
      return false;
    } catch {
      setServerStatus('error'); return false;
    }
  }

  useEffect(() => {
    // aquece na montagem
    pingServer();
  }, []);

  useEffect(() => {
    // aquece quando o usuário adiciona arquivos
    if (files.length > 0) pingServer();
  }, [files.length]);

  // ====== EDGE SUPABASE (CASO QUEIRA LIGAR JUNTO) ======
  async function triggerEdge(_jobId: string) {
    try {
      // opcional: chamar suas Supabase Edge Functions aqui se desejar
      // await supabase.functions.invoke('process-job', { body: { job_id: jobId } });
    } catch (e: any) {
      console.warn('Falha ao chamar Edge:', e?.message || e);
    }
  }

  // ====== SUPABASE ======
  async function uploadAllToSupabase(jobId: string, map: FileWithSchema[]) {
    const uploaded: { file_name: string; file_path: string; label?: string; schema: any }[] = [];
    for (const m of map) {
      const f = m.file;
      const path = `${jobId}/${crypto.randomUUID()}-${f.name}`;
      const { error } = await supabase.storage.from(BUCKET_DOCS).upload(path, f, {
        cacheControl: '3600',
        upsert: false,
        contentType: f.type || 'application/pdf'
      });
      if (error) throw new Error(`upload fail ${f.name}: ${error.message}`);
      uploaded.push({ file_name: f.name, file_path: path, label: m.label, schema: m.schema });
    }
    return uploaded;
  }

  async function createJob(total: number) {
    const { data, error } = await supabase.from('jobs').insert([{ total_count: total }]).select().single();
    if (error) throw error;
    return data as Job;
  }

  async function createJobItems(jobId: string, uploaded: { file_name: string; file_path: string; label?: string; schema: any }[]) {
    const rows = uploaded.map(u => ({
      job_id: jobId, file_name: u.file_name, file_path: u.file_path, schema: u.schema
    }));
    const { error } = await supabase.from('job_items').insert(rows);
    if (error) throw error;
  }

  function subscribeRealtime(jobId: string) {
    const ch1 = supabase
      .channel(`jobs-${jobId}`)
      .on('postgres_changes',
        { event: '*', schema: 'public', table: 'jobs', filter: `id=eq.${jobId}` },
        payload => setJob(prev => ({ ...(prev || (payload.new as any)), ...(payload.new as any) })))
      .subscribe();

    const ch2 = supabase
      .channel(`job_items-${jobId}`)
      .on('postgres_changes',
        { event: '*', schema: 'public', table: 'job_items', filter: `job_id=eq.${jobId}` },
        payload => {
          setItems(prev => {
            const idx = prev.findIndex(x => x.id === (payload.new as any).id);
            if (idx >= 0) {
              const clone = prev.slice(); clone[idx] = payload.new as any; return clone;
            }
            return [...prev, payload.new as any];
          });
        })
      .subscribe();

    return () => { supabase.removeChannel(ch1); supabase.removeChannel(ch2); };
  }

  function downloadUrlFor(path?: string | null) {
    if (!path) return null;
    const res = supabase.storage.from(BUCKET_RESULTS).getPublicUrl(path);
    const url =
      typeof res === 'string'
        ? res
        : (res?.data?.publicUrl || (res as any)?.publicUrl || null);
    return url;
  }

  // ====== COMBINAR JSONS QUANDO TERMINAR ======
  async function buildCombinedJsonIfDone(_curJobId: string) {
    try {
      if (!items || items.length === 0) return;

      const allDone = items.every(it => it.status === 'done' && it.result_path);
      if (!allDone) return;

      const urls = items.map(it => downloadUrlFor(it.result_path!)).filter(Boolean) as string[];
      if (urls.length === 0) return;

      const fetched = await Promise.all(
        urls.map(async (u) => {
          try { const r = await fetch(u); return await r.json(); } catch { return null; }
        })
      );

      const merged = fetched
        .map((j, idx) => ({ file: items[idx].file_name, result: j }))
        .filter(x => x.result !== null);

      const blob = new Blob([JSON.stringify(merged, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      setCombinedUrl(url);
    } catch (e) {
      console.warn('Falha ao montar JSON combinado:', e);
    }
  }

  useEffect(() => {
    if (job?.status === 'done') {
      buildCombinedJsonIfDone(job.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status, items.length]);

  // ====== START ======
  async function startProcessing() {
    try {
      setIsProcessing(true);
      setJob(null);
      setItems([]);
      setCombinedUrl(null);

      // acorda o servidor antes de começar
      await pingServer();

      const parsed = parseInput(schemaText);
      if (!parsed) { alert('JSON inválido.'); setIsProcessing(false); return; }

      const j = await createJob(files.length);
      setJob(j);
      const unsub = subscribeRealtime(j.id);

      const uploaded = await uploadAllToSupabase(j.id, mapping);
      await createJobItems(j.id, uploaded);

      // dispara seu worker no Fly (chamada simples POST /process-job)
      try {
        await fetch(`${FLY_API_URL}/process-job`, {
          method: 'POST',
          mode: 'cors',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ job_id: j.id })
        });
      } catch (e) {
        console.warn('Falha ao chamar Fly API:', (e as any)?.message || e);
      }

      // opcional: Supabase Edge também
      await triggerEdge(j.id);

      await supabase.from('jobs').update({ status: 'running' }).eq('id', j.id);

      const { data: initialItems } = await supabase.from('job_items').select('*').eq('job_id', j.id).order('created_at');
      setItems(initialItems || []);

      const endWatch = setInterval(async () => {
        const { data } = await supabase.from('jobs').select('*').eq('id', j.id).single();
        if (!data) return;
        if (data.status === 'done' || data.status === 'error') {
          clearInterval(endWatch);
          unsub();
          setIsProcessing(false);
        }
      }, 1500);

    } catch (e: any) {
      console.error(e);
      alert(e.message || 'Falha ao iniciar processamento');
      setIsProcessing(false);
    }
  }

  // ====== DRAG & DROP ======
  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(false);
    const f = Array.from(e.dataTransfer.files || []).filter((x) => x.type === 'application/pdf');
    if (f.length) setFiles(prev => [...prev, ...f]);
  }
  function onDragOver(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault(); setIsDragging(true);
  }
  function onDragLeave() { setIsDragging(false); }

  // ====== UI HELPERS ======
  const mappingIssues = useMemo(() => {
    const parsed = parseInput(schemaText);
    if (!parsed || parsed.mode === 'single') return [];
    const issues: string[] = [];
    const byName = new Set(parsed.items.map(i => basename(i.pdf_path || '').toLowerCase()).filter(Boolean));
    const fileNames = new Set(files.map(f => f.name.toLowerCase()));
    for (const n of byName) if (n && !fileNames.has(n)) issues.push(`Dataset (${n}) sem arquivo correspondente (por nome). Atribuído por ordem.`);
    for (const f of files) if (!byName.has(f.name.toLowerCase())) issues.push(`Arquivo ${f.name} não encontrado no dataset por nome. Atribuído por ordem.`);
    return issues;
  }, [files, schemaText]);

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-200">
      <header className="mx-auto max-w-6xl px-4 pt-5 pb-2">
        <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-1">
              <img src="public/limas.png" alt="Logo" className="h-32 w-auto inline-block align-middle" />
              <div>
                <h1
                  className="font-bold tracking-tight relative overflow-hidden"
                  style={{
                    fontSize: '50px', // ajuste aqui o tamanho em px
                    lineHeight: '1.1',
                    background: 'linear-gradient(90deg, #ffae35 20%, #fff7e0 50%, #ffae35 80%)',
                    backgroundSize: '200% 100%',
                    WebkitBackgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                    animation: 'shimmer 2s infinite linear',
                  }}
                >
                  Lima's PDF Extractor
                </h1>
              <style>
              {`
              @keyframes shimmer {
                0% { background-position: -100% 0; }
                100% { background-position: 100% 0; }
              }
              `}
              </style>
              <p className="text-base text-neutral-400 mt-1 animate-typing overflow-hidden whitespace-nowrap border-r-2 border-neutral-400 pr-2">
                Faça o upload dos seus PDFs para extrair informações relevantes.
              </p>
              <style>
              {`
              @keyframes typing {
                from { width: 0 }
                to { width: 100% }
              }
              .animate-typing {
                width: 0;
                animation: typing 2.5s steps(50, end) forwards;
              }
              `}
              </style>
              </div>
            </div>

          <div className="flex items-center gap-3">
            <button className="btn-secondary" onClick={pingServer} title="Acordar servidor (healthcheck)">
              Wake server
            </button>
            <div className="flex items-center gap-2">
              <span className={`size-3 rounded-full ${
                serverStatus === 'ok' ? 'bg-emerald-400 animate-pulse'
                : serverStatus === 'connecting' ? 'bg-amber-400 animate-pulse'
                : serverStatus === 'error' ? 'bg-rose-500'
                : 'bg-neutral-600'
              }`} />
                <span
                className={`text-xs px-2 py-1 rounded-full font-medium transition-all ${
                  serverStatus === 'ok'
                  ? 'bg-emerald-900 text-emerald-300 border border-emerald-400 shadow'
                  : serverStatus === 'connecting'
                  ? 'bg-amber-900 text-amber-300 border border-amber-400 shadow'
                  : serverStatus === 'error'
                  ? 'bg-rose-900 text-rose-300 border border-rose-400 shadow'
                  : 'bg-neutral-800 text-neutral-400 border border-neutral-600'
                }`}
                >
                {serverStatus === 'ok'
                  ? 'Conectado'
                  : serverStatus === 'connecting'
                  ? 'Conectando…'
                  : serverStatus === 'error'
                  ? 'Erro'
                  : 'Idle'}
                </span>
            </div>
            <div ref={spinnerRef} className="w-8 h-8 rounded-full border-4 border-indigo-500/80 border-t-transparent" />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 pb-10 grid lg:grid-cols-2 gap-6">
        {/* LEFT */}
        <section className="card-dark p-6">
          <h2 className="font-semibold mb-3">Documentos (PDF)</h2>

          {/* Drag and drop */}
          <div
            onDrop={onDrop}
            onDragOver={onDragOver}
            onDragLeave={onDragLeave}
            className={`rounded-2xl border-2 border-dashed px-4 py-10 text-center transition ${
              isDragging ? 'border-indigo-400 bg-indigo-400/10' : 'border-neutral-700 bg-neutral-900/40'
            }`}
          >
            <p className="text-neutral-300 mb-2">Arraste seus PDFs aqui</p>
            <p className="text-xs text-neutral-500 mb-4">ou</p>
            <button className="btn-secondary" onClick={() => fileInputRef.current?.click()}>
              Selecionar arquivos
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="application/pdf"
              onChange={e => setFiles(prev => [...prev, ...Array.from(e.target.files || [])])}
              className="hidden"
            />
          </div>

          {files.length > 0 && (
            <ul className="mt-4 text-sm space-y-1">
              {files.map(f => (
                <li key={f.name} className="flex items-center justify-between">
                  <span className="truncate">{f.name}</span>
                  <span className="text-neutral-500">{(f.size / 1024).toFixed(1)} KB</span>
                </li>
              ))}
            </ul>
          )}

          <h2 className="font-semibold mt-6 mb-2">Schema / Dataset JSON</h2>
          <p className="text-xs text-neutral-400 mb-2">
            Aceita <b>objeto</b> (schema único) ou <b>array</b> ({'{ label, extraction_schema, pdf_path? }'}).
          </p>
          <textarea
            className="textarea-dark"
            value={schemaText}
            onChange={e => setSchemaText(e.target.value)}
            placeholder='Ex.: { "nome": null }  ou  [ { "label":"cnh", "extraction_schema":{...}, "pdf_path":"cnh_1.pdf" } ]'
          />

          {/* Preview do mapeamento */}
          {mapping.length > 0 && (
            <div className="mt-4">
              <h3 className="font-medium mb-2">Preview do mapeamento</h3>
              <div className="rounded-xl border border-neutral-800 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-neutral-900/70">
                    <tr>
                      <th className="text-left p-2">Arquivo</th>
                      <th className="text-left p-2">Label</th>
                      <th className="text-left p-2">Campos</th>
                      <th className="text-left p-2">Casou por</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mapping.map(m => (
                      <tr key={m.file.name} className="border-t border-neutral-800">
                        <td className="p-2">{m.file.name}</td>
                        <td className="p-2">
                          {m.label ? <span className="badge badge-ok">{m.label}</span> : <span className="text-neutral-500">—</span>}
                        </td>
                        <td className="p-2 text-neutral-300">{Object.keys(m.schema || {}).join(', ') || '—'}</td>
                        <td className="p-2">
                          {m.matchedBy === 'filename' && <span className="badge badge-ok">filename</span>}
                          {m.matchedBy === 'order' && <span className="badge badge-warn">ordem</span>}
                          {m.matchedBy === 'single' && <span className="badge badge-run">schema único</span>}
                        </td>
                      </tr>
                    ))}
                    {mapping.length === 0 && (
                      <tr><td className="p-2 text-neutral-500" colSpan={4}>—</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              {mappingIssues.length > 0 && (
                <ul className="mt-3 space-y-1 text-xs text-amber-400">
                  {mappingIssues.map((msg, i) => <li key={i} className="badge badge-warn">{msg}</li>)}
                </ul>
              )}
            </div>
          )}
        </section>
        

        {/* RIGHT */}
        <section className="card-dark p-6">
          <h2 className="font-semibold mb-3">Processamento</h2>
          <div className="flex items-center gap-3">
            <button className="btn-primary" disabled={!canStart || isProcessing} onClick={startProcessing}>
              {isProcessing ? 'Processando…' : 'Processar'}
            </button>
            <span className="text-sm text-neutral-400">Job: {job?.id || '—'}</span>
            {combinedUrl && (
              <a className="btn-ghost" href={combinedUrl} download={`job-${job?.id}-combined.json`}>
                Baixar combinado
              </a>
            )}
          </div>

          <div className="mt-6">
            <div className="flex items-center justify-between text-sm">
              <div>Status: <b className="text-neutral-100">{job?.status || 'aguardando'}</b></div>
              <div className="text-neutral-500">{job?.done_count ?? 0}/{job?.total_count ?? 0}</div>
            </div>
            <div className="w-full h-3 bg-neutral-900 rounded-xl mt-2 overflow-hidden">
              <div className="h-3 rounded-xl bg-gradient-to-r from-indigo-500 to-indigo-600 transition-all" style={{ width: `${percent}%` }} />
            </div>
          </div>

          <h3 className="font-semibold mt-6">Itens</h3>
          <div className="mt-2 max-h-80 overflow-auto rounded-xl ring-1 ring-neutral-800">
            <table className="w-full text-sm">
              <thead className="bg-neutral-900/70">
                <tr>
                  <th className="text-left p-2">Arquivo</th>
                  <th className="text-left p-2">Status</th>
                  <th className="text-left p-2">Tempo</th>
                  <th className="text-left p-2">Resultado</th>
                </tr>
              </thead>
              <tbody>
                {items.map(it => (
                  <tr key={it.id} className="border-t border-neutral-800">
                    <td className="p-2">{it.file_name}</td>
                    <td className="p-2">
                      {it.status === 'done' && <span className="badge badge-ok">done</span>}
                      {it.status === 'running' && <span className="badge badge-run">running</span>}
                      {it.status === 'queued' && <span className="badge">queued</span>}
                      {it.status === 'error' && <span className="badge badge-err" title={it.error_message || ''}>error</span>}
                    </td>
                    <td className="p-2">{prettyMs(it.duration_ms)}</td>
                    <td className="p-2">
                      {it.result_path ? (
                        <a className="text-indigo-400 underline" href={downloadUrlFor(it.result_path)!} target="_blank" rel="noreferrer">
                          JSON
                        </a>
                      ) : '—'}
                    </td>
                  </tr>
                ))}
                {items.length === 0 && (
                  <tr><td className="p-2 text-neutral-500" colSpan={4}>Nenhum item ainda…</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </main>

      <footer className="mx-auto max-w-6xl px-6 pb-10 text-xs text-neutral-500">
        Todos os direitos reservados. Desenvolvido por Matheus Lima.
      </footer>
    </div>
  );
}
