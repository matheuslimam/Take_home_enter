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

const prettyMs = (ms?: number | null) =>
  !ms && ms !== 0 ? '—' : `${(ms / 1000).toFixed(2)}s`;

export default function App() {
  const [files, setFiles] = useState<File[]>([]);
  const [schemaText, setSchemaText] = useState<string>('{\n  "nome": null\n}');
  const [job, setJob] = useState<Job | null>(null);
  const [items, setItems] = useState<JobItem[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const spinnerRef = useRef<HTMLDivElement>(null);

  // Spinner animado
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

  const canStart = useMemo(() => files.length > 0 && schemaText.trim().startsWith('{'), [files, schemaText]);

  async function uploadAllToSupabase(jobId: string) {
    // cria uma pasta jobId/ no bucket docs
    const uploaded: { file_name: string; file_path: string }[] = [];

    for (const f of files) {
      const path = `${jobId}/${crypto.randomUUID()}-${f.name}`;
      const { error } = await supabase.storage.from(BUCKET_DOCS).upload(path, f, {
        cacheControl: '3600',
        upsert: false,
        contentType: f.type || 'application/pdf'
      });
      if (error) throw new Error(`upload fail ${f.name}: ${error.message}`);
      uploaded.push({ file_name: f.name, file_path: path });
    }
    return uploaded;
  }

  async function createJob(total: number) {
    const { data, error } = await supabase.from('jobs').insert([{ total_count: total }]).select().single();
    if (error) throw error;
    return data as Job;
  }

  async function createJobItems(jobId: string, uploaded: { file_name: string; file_path: string }[], schemaJson: any) {
    const rows = uploaded.map(u => ({
      job_id: jobId,
      file_name: u.file_name,
      file_path: u.file_path,
      schema: schemaJson
    }));
    const { error } = await supabase.from('job_items').insert(rows);
    if (error) throw error;
  }

  function subscribeRealtime(jobId: string) {
    // Jobs
    const ch1 = supabase
      .channel(`jobs-${jobId}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'jobs', filter: `id=eq.${jobId}` },
        payload => {
          setJob(prev => ({ ...(prev || (payload.new as any)), ...(payload.new as any) }));
        }
      )
      .subscribe();

    // Items
    const ch2 = supabase
      .channel(`job_items-${jobId}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'job_items', filter: `job_id=eq.${jobId}` },
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
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(ch1);
      supabase.removeChannel(ch2);
    };
  }

  async function startProcessing() {
    try {
      setIsProcessing(true);
      setJob(null);
      setItems([]);

      // valida JSON
      let schemaJson: any;
      try { schemaJson = JSON.parse(schemaText); } catch { alert('Schema JSON inválido.'); return; }

      // cria job
      const j = await createJob(files.length);
      setJob(j);
      const unsub = subscribeRealtime(j.id);

      // faz upload dos PDFs ao bucket docs
      const uploaded = await uploadAllToSupabase(j.id);

      // cria job_items
      await createJobItems(j.id, uploaded, schemaJson);

      // “sinaliza” job como running
      await supabase.from('jobs').update({ status: 'running' }).eq('id', j.id);

      // fallback polling da lista inicial (opcional)
      const { data: initialItems } = await supabase.from('job_items').select('*').eq('job_id', j.id).order('created_at');
      setItems(initialItems || []);

      // auto-cleanup unsub quando finalizar
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

  function downloadUrlFor(path?: string | null) {
    if (!path) return null;
    // bucket results é público; para privado, use getPublicUrl() ou createSignedUrl()
    const { data } = supabase.storage.from(BUCKET_RESULTS).getPublicUrl(path);
    return data.publicUrl;
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="max-w-5xl mx-auto p-6">
        <h1 className="text-2xl font-bold">Extractor UI — Take-home</h1>
        <p className="text-sm text-slate-600">Upload → Processar → Progresso em tempo real → JSON final</p>
      </header>

      <main className="max-w-5xl mx-auto p-6 grid md:grid-cols-2 gap-6">
        <section className="bg-white rounded-2xl shadow p-6">
          <h2 className="font-semibold mb-3">1) Documentos (PDF)</h2>
          <input
            type="file"
            multiple
            accept="application/pdf"
            onChange={e => setFiles(Array.from(e.target.files || []))}
          />
          <ul className="mt-3 text-sm list-disc pl-5">
            {files.map(f => <li key={f.name}>{f.name} <span className="text-slate-400">({(f.size / 1024).toFixed(1)} KB)</span></li>)}
          </ul>

          <h2 className="font-semibold mt-6 mb-3">2) Schema JSON</h2>
          <textarea
            className="w-full h-48 border rounded-lg p-2 font-mono text-sm"
            value={schemaText}
            onChange={e => setSchemaText(e.target.value)}
          />
        </section>

        <section className="bg-white rounded-2xl shadow p-6">
          <h2 className="font-semibold mb-3">3) Processamento</h2>
          <div className="flex items-center gap-4">
            <button
              className={`px-5 py-3 rounded-lg text-white ${canStart ? 'bg-indigo-600 hover:bg-indigo-700' : 'bg-slate-300'}`}
              disabled={!canStart || isProcessing}
              onClick={startProcessing}
            >
              {isProcessing ? 'Processando...' : 'Processar'}
            </button>
            <div ref={spinnerRef} className="w-8 h-8 rounded-full border-4 border-indigo-600 border-t-transparent" />
          </div>

          <div className="mt-6">
            <div className="text-sm text-slate-600">Job: {job?.id || '—'} | Status: <b>{job?.status || 'aguardando'}</b></div>
            <div className="w-full bg-slate-100 h-2 rounded mt-2">
              <div
                className="bg-indigo-600 h-2 rounded"
                style={{
                  width: job && job.total_count
                    ? `${Math.min(100, Math.round((job.done_count / job.total_count) * 100))}%`
                    : '0%'
                }}
              />
            </div>
          </div>

          <h3 className="font-semibold mt-6">Itens</h3>
          <div className="mt-2 max-h-64 overflow-auto border rounded-lg">
            <table className="w-full text-sm">
              <thead className="bg-slate-50">
                <tr>
                  <th className="text-left p-2">Arquivo</th>
                  <th className="text-left p-2">Status</th>
                  <th className="text-left p-2">Tempo</th>
                  <th className="text-left p-2">Resultado</th>
                </tr>
              </thead>
              <tbody>
                {items.map(it => (
                  <tr key={it.id} className="border-t">
                    <td className="p-2">{it.file_name}</td>
                    <td className="p-2">{it.status}</td>
                    <td className="p-2">{prettyMs(it.duration_ms)}</td>
                    <td className="p-2">
                      {it.result_path ? (
                        <a className="text-indigo-600 underline" href={downloadUrlFor(it.result_path)!} target="_blank" rel="noreferrer">
                          JSON
                        </a>
                      ) : (it.status === 'error' ? <span className="text-rose-600" title={it.error_message || ''}>erro</span> : '—')}
                    </td>

                  </tr>
                ))}
                {items.length === 0 && (
                  <tr><td className="p-2 text-slate-400" colSpan={4}>Nenhum item ainda…</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </main>

      <footer className="max-w-5xl mx-auto p-6 text-xs text-slate-500">
        Feito para o take-home — Supabase (Storage/DB/Realtime) + Render Worker + GitHub Pages
      </footer>
    </div>
  );
}
