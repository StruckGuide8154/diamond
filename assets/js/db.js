// Supabase data layer. Requires config.js and the supabase-js CDN
// script to be loaded first. All writes are governed by row-level
// security policies defined in supabase/setup.sql.
(function () {
  const cfg = window.DIAMOND_CONFIG || {};
  const ready = !!(cfg.SUPABASE_URL && cfg.SUPABASE_ANON_KEY && window.supabase);
  const client = ready ? window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY) : null;

  window.DB = {
    ready,
    client,

    // --- public (visitor) actions -------------------------------
    async sendMessage({ name, email, phone, treatment, message }) {
      const { error } = await client.from("messages").insert({ name, email, phone, treatment, message });
      if (error) throw error;
    },

    async placeOrder({ name, email, phone, notes, items, total }) {
      const { error } = await client.from("orders").insert({ name, email, phone, notes, items, total });
      if (error) throw error;
    },

    // --- admin actions (require a signed-in session) ------------
    async signIn(email, password) {
      const { data, error } = await client.auth.signInWithPassword({ email, password });
      if (error) throw error;
      return data;
    },

    async signOut() { await client.auth.signOut(); },

    async session() {
      const { data } = await client.auth.getSession();
      return data.session;
    },

    async list(table) {
      const { data, error } = await client.from(table).select("*").order("created_at", { ascending: false });
      if (error) throw error;
      return data;
    },

    async setStatus(table, id, status) {
      const { error } = await client.from(table).update({ status }).eq("id", id);
      if (error) throw error;
    },

    async remove(table, id) {
      const { error } = await client.from(table).delete().eq("id", id);
      if (error) throw error;
    }
  };
})();
