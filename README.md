# Diamond Beauty

Multi-page site for Diamond Beauty & Hair Clinic (Nottingham): Home, Treatments,
Our Clinic, Boutique, Product Detail, Contact/Booking, Checkout — plus an
**admin dashboard** at `admin.html`.

**Live site:** https://struckguide8154.github.io/diamond/
(also served at http://rooted.cloud/diamond/ via the account's custom Pages domain)

**Admin dashboard:** https://struckguide8154.github.io/diamond/admin.html

## How it works

- **Hosting:** GitHub Pages (this repo, `main` branch). Every push to `main`
  redeploys the site automatically.
- **Database:** [Supabase](https://supabase.com) free tier (managed Postgres).
  Booking requests from the contact page and order requests from checkout are
  saved there, so **data persists forever** — it lives in the database, not on
  the site, and is untouched by site updates or redeploys.
- **Admin:** `admin.html` — sign in with your Supabase admin account to view,
  update and delete booking requests and orders.
- **Security:** the browser only ever holds the public *anon* key. Row-level
  security means visitors can *submit* messages/orders but never read them;
  only signed-in admins can.
- **Keep-alive:** `.github/workflows/keepalive.yml` pings the database every
  3 days so the free Supabase project is never paused for inactivity.

Until the database is configured, the contact and checkout forms gracefully
fall back to opening an email to the clinic instead.

## One-time database setup (~5 minutes)

1. Go to https://supabase.com → sign in with GitHub → **New project**
   (free plan). Pick any name, e.g. `diamond-beauty`, region `West EU (London)`.
2. When the project is ready, open **SQL Editor**, paste the contents of
   [`supabase/setup.sql`](supabase/setup.sql), and click **Run**.
3. Go to **Authentication → Sign In / Providers** and turn **OFF**
   "Allow new users to sign up". *(Important — otherwise anyone could
   register and see the admin data.)*
4. Go to **Authentication → Users → Add user** and create your admin login
   (your email + a strong password). Tick "Auto confirm user".
5. Go to **Project Settings → API** and copy:
   - **Project URL**
   - **anon / public key**
6. Paste both into [`assets/js/config.js`](assets/js/config.js), commit and push:

   ```js
   window.DIAMOND_CONFIG = {
     SUPABASE_URL: "https://YOURPROJECT.supabase.co",
     SUPABASE_ANON_KEY: "eyJ..."
   };
   ```

That's it. The contact form and checkout now save to the database, and
https://struckguide8154.github.io/diamond/admin.html is your dashboard.

## Local preview

Just open `index.html` in a browser, or run any static server, e.g.:

```
python -m http.server
```
