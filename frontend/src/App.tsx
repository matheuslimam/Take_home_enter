import { useEffect, useMemo, useRef, useState } from 'react';
import { supabase, BUCKET_DOCS, BUCKET_RESULTS } from './lib/supabase';

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

const prettyMs = (ms?: number | null) =>
  !ms && ms !== 0 ? '—' : `${(ms / 1000).toFixed(2)}s`;

function basename(p?: string) {
  if (!p) return '';
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

function parseInput(text: string): ParsedInput | null {
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      // valida estrutura { label, extraction_schema, pdf_path? }
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

export default function App() {
  const [files, setFiles] = useState<File[]>([]);
  const [schemaText, setSchemaText] = useState<string>(
    '{\n  "nome": null\n}'
  );
  const [job, setJob] = useState<Job | null>(null);
  const [items, setItems] = useState<JobItem[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [mapping, setMapping] = useState<FileWithSchema[]>([]);
  const spinnerRef = useRef<HTMLDivElement>(null);

  // Spinner manual
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
    return () => {
      if (id !== undefined) {
        cancelAnimationFrame(id);
      }
    };
  }, [isProcessing]);

  // Monta o mapeamento (arquivo → schema/label) sempre que files ou schemaText mudarem
  useEffect(() => {
    const parsed = parseInput(schemaText);
    if (!parsed) {
      setMapping([]);
      return;
    }
    if (parsed.mode === 'single') {
      setMapping(files.map((f) => ({
        file: f,
        schema: parsed.schema,
        matchedBy: 'single',
      })));
      return;
    }
    // labeled
    const labels = parsed.items;
    // index por filename (basename do pdf_path)
    const byName = new Map<string, LabeledSchema>();
    labels.forEach((it) => {
      const b = basename(it.pdf_path || '');
      if (b) byName.set(b.toLowerCase(), it);
    });

    const next: FileWithSchema[] = [];
    const leftovers = labels.filter((it) => !it.pdf_path); // sem pdf_path
    const unused = new Set(leftovers.map((_, i) => i)); // índices livres por ordem

    files.forEach((f) => {
      const hit = byName.get(f.name.toLowerCase());
      if (hit) {
        next.push({
          file: f,
          schema: hit.extraction_schema,
          label: hit.label,
          matchedBy: 'filename',
        });
      } else {
        // pega próximo schema disponível por ordem (se faltar, usa o primeiro)
        let idx: number | undefined = undefined;
        for (const i of unused) { idx = i; break; }
        if (idx !== undefined) {
          const it = leftovers[idx];
          unused.delete(idx);
          next.push({
            file: f,
            schema: it.extraction_schema,
            label: it.label,
            matchedBy: 'order',
          });
        } else if (labels.length > 0) {
          // fallback: usa o primeiro da lista
          next.push({
            file: f,
            schema: labels[0].extraction_schema,
            label: labels[0].label,
            matchedBy: 'order',
          });
        } else {
          // não há schemas válidos — fica vazio
          next.push({
            file: f,
            schema: {},
            matchedBy: 'order',
          });
        }
      }
    });

    setMapping(next);
  }, [files, schemaText]);

  const parsedOk = useMemo(() => !!parseInput(schemaText), [schemaText]);
  const canStart = useMemo(
    () => files.length > 0 && parsedOk,
    [files, parsedOk]
  );

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
      uploaded.push({
        file_name: f.name,
        file_path: path,
        label: m.label,
        schema: m.schema,
      });
    }
    return uploaded;
  }

  async function createJob(total: number) {
    const { data, error } = await supabase.from('jobs').insert([{ total_count: total }]).select().single();
    if (error) throw error;
    return data as Job;
  }

  async function createJobItems(jobId: string, uploaded: { file_name: string; file_path: string; label?: string; schema: any }[]) {
    // Banco não tem coluna 'label', então guardamos label apenas no estado da UI.
    // Cada item leva seu schema específico.
    const rows = uploaded.map(u => ({
      job_id: jobId,
      file_name: u.file_name,
      file_path: u.file_path,
      schema: u.schema
    }));
    const { error } = await supabase.from('job_items').insert(rows);
    if (error) throw error;
    // retornamos um mapa filename->label p/ UI manter a exibição
    return new Map(uploaded.map(u => [u.file_name, u.label]));
  }

  function subscribeRealtime(jobId: string) {
    const ch1 = supabase
      .channel(`jobs-${jobId}`)
      .on('postgres_changes', { event: '*', schema: 'public', table: 'jobs', filter: `id=eq.${jobId}` },
        payload => setJob(prev => ({ ...(prev || (payload.new as any)), ...(payload.new as any) })))
      .subscribe();

    const ch2 = supabase
      .channel(`job_items-${jobId}`)
      .on('postgres_changes', { event: '*', schema: 'public', table: 'job_items', filter: `job_id=eq.${jobId}` },
        payload => {
          setItems(prev => {
            const idx = prev.findIndex(x => x.id === (payload.new as any).id);
            if (idx >= 0) {
              const clone = prev.slice();
              clone[idx] = payload.new as any;
              return clone;
            }
            return [...prev, payload.new as any];
          });
        })
      .subscribe();

    return () => { supabase.removeChannel(ch1); supabase.removeChannel(ch2); };
  }

  async function startProcessing() {
    try {
      setIsProcessing(true);
      setJob(null);
      setItems([]);

      const parsed = parseInput(schemaText);
      if (!parsed) { alert('JSON inválido.'); setIsProcessing(false); return; }

      const j = await createJob(files.length);
      setJob(j);
      const unsub = subscribeRealtime(j.id);

      const uploaded = await uploadAllToSupabase(j.id, mapping);
      await createJobItems(j.id, uploaded);

      await supabase.from('jobs').update({ status: 'running' }).eq('id', j.id);

      const { data: initialItems } = await supabase.from('job_items').select('*').eq('job_id', j.id).order('created_at');
      setItems(initialItems || []);

      // monitorar fim
      const endWatch = setInterval(async () => {
        const { data } = await supabase.from('jobs').select('*').eq('id', j.id).single();
        if (!data) return;
        if (data.status === 'done' || data.status === 'error') {
          clearInterval(endWatch);
          unsub();
          setIsProcessing(false);
        }
      }, 1500);

      // anexa labels na memória (UI-only)
      // (como só exibimos, não persistimos no banco)
      // já temos 'mapping' para consultar label por filename quando renderizar

    } catch (e: any) {
      console.error(e);
      alert(e.message || 'Falha ao iniciar processamento');
      setIsProcessing(false);
    }
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

  // helpers visual
  const percent = job && job.total_count ? Math.min(100, Math.round((job.done_count / job.total_count) * 100)) : 0;
  const mappingIssues = useMemo(() => {
    const parsed = parseInput(schemaText);
    if (!parsed || parsed.mode === 'single') return [];
    const issues: string[] = [];
    const byName = new Set(parsed.items.map(i => basename(i.pdf_path || '').toLowerCase()).filter(Boolean));
    const fileNames = new Set(files.map(f => f.name.toLowerCase()));
    // labels com pdf_path não encontrado
    for (const n of byName) if (n && !fileNames.has(n)) issues.push(`Item do dataset (${n}) não tem arquivo correspondente (por nome). Atribuído por ordem.`);
    // arquivos sem casadinha por nome
    for (const f of files) if (!byName.has(f.name.toLowerCase())) issues.push(`Arquivo ${f.name} não encontrado no dataset por nome. Atribuído por ordem.`);
    return issues;
  }, [files, schemaText]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 text-slate-800">
      <header className="mx-auto max-w-6xl px-6 pt-8 pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">Extractor UI — Take-home</h1>
            <p className="text-sm text-slate-600">Upload → Processar → Progresso em tempo real → JSON final</p>
          </div>
          <div className="relative">
            <div ref={spinnerRef} className="w-10 h-10 rounded-full border-4 border-indigo-600 border-t-transparent" />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 pb-10 grid lg:grid-cols-2 gap-6">
        {/* LEFT */}
        <section className="card p-6">
          <h2 className="font-semibold mb-3">1) Documentos (PDF)</h2>
          <label className="block">
            <input
              type="file"
              multiple
              accept="application/pdf"
              onChange={e => setFiles(Array.from(e.target.files || []))}
              className="block w-full text-sm file:mr-4 file:rounded-lg file:border-0 file:bg-indigo-50 file:px-4 file:py-2 file:text-indigo-700 hover:file:bg-indigo-100"
            />
          </label>

          {files.length > 0 && (
            <ul className="mt-3 text-sm space-y-1">
              {files.map(f => (
                <li key={f.name} className="flex items-center justify-between">
                  <span className="truncate">{f.name}</span>
                  <span className="text-slate-400">{(f.size / 1024).toFixed(1)} KB</span>
                </li>
              ))}
            </ul>
          )}

          <h2 className="font-semibold mt-6 mb-2">2) Schema / Dataset JSON</h2>
          <p className="text-xs text-slate-500 mb-2">
            Aceita <b>objeto</b> (schema único) ou <b>array</b> no formato do dataset ({'{ label, extraction_schema, pdf_path? }'}).
          </p>
          <textarea
            className="textarea"
            value={schemaText}
            onChange={e => setSchemaText(e.target.value)}
            placeholder='Ex.: { "nome": null }  ou  [ { "label":"cnh", "extraction_schema":{...}, "pdf_path":"cnh_1.pdf" } ]'
          />

          {/* Preview do mapeamento */}
          {mapping.length > 0 && (
            <div className="mt-4">
              <h3 className="font-medium mb-2">Preview do mapeamento</h3>
              <div className="rounded-xl border border-slate-200 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="text-left p-2">Arquivo</th>
                      <th className="text-left p-2">Label</th>
                      <th className="text-left p-2">Campos</th>
                      <th className="text-left p-2">Casou por</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mapping.map(m => (
                      <tr key={m.file.name} className="border-t">
                        <td className="p-2">{m.file.name}</td>
                        <td className="p-2">
                          {m.label ? <span className="badge badge-ok">{m.label}</span> : <span className="text-slate-400">—</span>}
                        </td>
                        <td className="p-2 text-slate-600">{Object.keys(m.schema || {}).join(', ') || '—'}</td>
                        <td className="p-2">
                          {m.matchedBy === 'filename' && <span className="badge badge-ok">filename</span>}
                          {m.matchedBy === 'order' && <span className="badge badge-warn">ordem</span>}
                          {m.matchedBy === 'single' && <span className="badge badge-run">schema único</span>}
                        </td>
                      </tr>
                    ))}
                    {mapping.length === 0 && (
                      <tr><td className="p-2 text-slate-400" colSpan={4}>—</td></tr>
                    )}
                  </tbody>
                </table>
              </div>

              {mappingIssues.length > 0 && (
                <ul className="mt-3 space-y-1 text-xs text-amber-700">
                  {mappingIssues.map((msg, i) => <li key={i} className="badge badge-warn">{msg}</li>)}
                </ul>
              )}
            </div>
          )}
        </section>

        {/* RIGHT */}
        <section className="card p-6">
          <h2 className="font-semibold mb-3">3) Processamento</h2>
          <div className="flex items-center gap-3">
            <button
              className="btn-primary"
              disabled={!canStart || isProcessing}
              onClick={startProcessing}
            >
              {isProcessing ? 'Processando…' : 'Processar'}
            </button>
            <span className="text-sm text-slate-600">Job: {job?.id || '—'}</span>
          </div>

          <div className="mt-6">
            <div className="flex items-center justify-between text-sm">
              <div>Status: <b>{job?.status || 'aguardando'}</b></div>
              <div className="text-slate-500">{job?.done_count ?? 0}/{job?.total_count ?? 0}</div>
            </div>
            <div className="w-full h-3 bg-slate-100 rounded-xl mt-2 overflow-hidden">
              <div className="h-3 rounded-xl bg-gradient-to-r from-indigo-500 to-indigo-600 transition-all" style={{ width: `${percent}%` }} />
            </div>
          </div>

          <h3 className="font-semibold mt-6">Itens</h3>
          <div className="mt-2 max-h-80 overflow-auto rounded-xl ring-1 ring-slate-200">
            <table className="w-full text-sm">
              <thead className="bg-slate-50">
                <tr>
                  <th className="text-left p-2">Arquivo</th>
                  <th className="text-left p-2">Label</th>
                  <th className="text-left p-2">Status</th>
                  <th className="text-left p-2">Tempo</th>
                  <th className="text-left p-2">Resultado</th>
                </tr>
              </thead>
              <tbody>
                {items.map(it => {
                  const mapRow = mapping.find(m => m.file.name === it.file_name);
                  const label = mapRow?.label;
                  return (
                    <tr key={it.id} className="border-t">
                      <td className="p-2">{it.file_name}</td>
                      <td className="p-2">{label ? <span className="badge badge-ok">{label}</span> : <span className="text-slate-400">—</span>}</td>
                      <td className="p-2">
                        {it.status === 'done' && <span className="badge badge-ok">done</span>}
                        {it.status === 'running' && <span className="badge badge-run">running</span>}
                        {it.status === 'queued' && <span className="badge">queued</span>}
                        {it.status === 'error' && <span className="badge badge-err" title={it.error_message || ''}>error</span>}
                      </td>
                      <td className="p-2">{prettyMs(it.duration_ms)}</td>
                      <td className="p-2">
                        {it.result_path ? (
                          <a className="text-indigo-600 underline" href={downloadUrlFor(it.result_path)!} target="_blank" rel="noreferrer">
                            JSON
                          </a>
                        ) : '—'}
                      </td>
                    </tr>
                  );
                })}
                {items.length === 0 && (
                  <tr><td className="p-2 text-slate-400" colSpan={5}>Nenhum item ainda…</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </main>

      <footer className="mx-auto max-w-6xl px-6 pb-10 text-xs text-slate-500">
        Feito para o take-home — Supabase (Storage/DB/Realtime) + Render Worker + GitHub Pages
      </footer>
    </div>
  );
}
