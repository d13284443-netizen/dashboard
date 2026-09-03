// config.js — Public Supabase credentials for the dashboard.
//
// SAFE TO COMMIT AND SERVE TO BROWSERS. The anon key is designed to be
// public: every row it can reach is gated by the Row-Level Security
// policies in schema/010, which only grant SELECT to authenticated
// users. It is NOT the service_role key — that one bypasses RLS, lives
// only in the VPS worker's .env, and must never appear in any file that
// reaches a browser.
window.IV_CONFIG = {
  SUPABASE_URL: "https://ktmkewyjvnweqnzzzayc.supabase.co",
  SUPABASE_ANON_KEY: "sb_publishable_QUvwt5P7NIGSNOoO_A6SSg_9jOighbK",

  // Poll interval for live data while a tab is visible. Data only
  // changes every 15-20 min on ingest, so there is no point hammering
  // the API — this refreshes a few times between updates to catch new
  // rows without wasting requests.
  REFRESH_SECONDS: 60,
};