import { createClient } from '@supabase/supabase-js';

export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL!,
  import.meta.env.VITE_SUPABASE_ANON_KEY!,
  { auth: { persistSession: false } }
);

export const BUCKET_DOCS = import.meta.env.VITE_BUCKET_DOCS || 'docs';
export const BUCKET_RESULTS = import.meta.env.VITE_BUCKET_RESULTS || 'results';
