-- ============================================================
-- Diamond Beauty — database setup
-- Run this once in the Supabase SQL Editor (Dashboard > SQL).
-- Safe to re-run: uses IF NOT EXISTS / OR REPLACE where possible.
-- ============================================================

-- Contact / booking requests -------------------------------------
create table if not exists public.messages (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  name        text not null,
  email       text not null,
  phone       text,
  treatment   text,
  message     text,
  status      text not null default 'new'   -- new | handled
);

-- Boutique order requests ----------------------------------------
create table if not exists public.orders (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  name        text not null,
  email       text not null,
  phone       text,
  notes       text,
  items       jsonb not null default '[]',  -- [{id, name, qty, price}]
  total       numeric(10,2) not null default 0,
  status      text not null default 'new'   -- new | confirmed | fulfilled | cancelled
);

alter table public.messages enable row level security;
alter table public.orders   enable row level security;

-- Visitors (anon key) may ONLY insert — never read, update or delete.
drop policy if exists "anon can submit messages" on public.messages;
create policy "anon can submit messages" on public.messages
  for insert to anon with check (true);

drop policy if exists "anon can submit orders" on public.orders;
create policy "anon can submit orders" on public.orders
  for insert to anon with check (true);

-- Signed-in admins get full access.
drop policy if exists "admins manage messages" on public.messages;
create policy "admins manage messages" on public.messages
  for all to authenticated using (true) with check (true);

drop policy if exists "admins manage orders" on public.orders;
create policy "admins manage orders" on public.orders
  for all to authenticated using (true) with check (true);

-- IMPORTANT: in the Supabase dashboard go to
--   Authentication > Sign In / Up  and turn OFF "Allow new users to sign up",
-- then add your admin account under Authentication > Users > Add user.
-- Otherwise anyone could self-register and count as "authenticated".
