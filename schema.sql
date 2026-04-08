-- Supabase Postgres schema for OJTTimer
-- Apply in Supabase SQL Editor (or via migrations) before deploying to Vercel.
--
-- Durability: Vercel serverless has no persistent local disk. All relational data
-- must live in this database (via DATABASE_URL / pooler). Binary assets use
-- Supabase Storage, not the app's filesystem.

create table if not exists public.batches (
  id bigserial primary key,
  name text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.ojt_users (
  id bigserial primary key,
  sr_code text not null unique,
  name text not null,
  gender text not null check (gender in ('Male', 'Female')),
  department text not null,
  course text not null,
  batch_id bigint not null references public.batches(id) on delete restrict,
  required_hours double precision not null check (required_hours > 0),
  password_hash text not null,
  photo_filename text not null default '',
  extra_photo_filename text not null default '',
  goal_text text not null default '',
  accomplishment_text text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists idx_ojt_users_batch on public.ojt_users(batch_id);
create index if not exists idx_ojt_users_sr on public.ojt_users(sr_code);

create table if not exists public.time_entries (
  id bigserial primary key,
  user_id bigint not null references public.ojt_users(id) on delete cascade,
  time_in text not null,
  time_out text,
  session_note text not null default '',
  time_in_method text,
  time_out_method text
);

create index if not exists idx_time_entries_user on public.time_entries(user_id);

-- ---------------------------------------------------------------------------
-- Row Level Security (RLS)
--
-- Supabase Data API (REST) uses roles `anon` (JWT anon key) and `authenticated`
-- (logged-in users). Policies below block those roles from reading/writing tables
-- directly. Your Flask app uses DATABASE_URL as the database user `postgres`
-- (or pooler equivalent), which bypasses RLS — so your existing Python queries
-- keep working.
--
-- The `service_role` JWT bypasses RLS; never put it in browser code.
-- ---------------------------------------------------------------------------

alter table public.batches enable row level security;
alter table public.ojt_users enable row level security;
alter table public.time_entries enable row level security;

-- Idempotent: drop then recreate policies (safe to re-run in SQL Editor).

-- batches
drop policy if exists "batches_block_anon" on public.batches;
drop policy if exists "batches_block_authenticated" on public.batches;
create policy "batches_block_anon"
  on public.batches
  for all
  to anon
  using (false)
  with check (false);
create policy "batches_block_authenticated"
  on public.batches
  for all
  to authenticated
  using (false)
  with check (false);

-- ojt_users (includes password_hash — must never be exposed via anon API)
drop policy if exists "ojt_users_block_anon" on public.ojt_users;
drop policy if exists "ojt_users_block_authenticated" on public.ojt_users;
create policy "ojt_users_block_anon"
  on public.ojt_users
  for all
  to anon
  using (false)
  with check (false);
create policy "ojt_users_block_authenticated"
  on public.ojt_users
  for all
  to authenticated
  using (false)
  with check (false);

-- time_entries
drop policy if exists "time_entries_block_anon" on public.time_entries;
drop policy if exists "time_entries_block_authenticated" on public.time_entries;
create policy "time_entries_block_anon"
  on public.time_entries
  for all
  to anon
  using (false)
  with check (false);
create policy "time_entries_block_authenticated"
  on public.time_entries
  for all
  to authenticated
  using (false)
  with check (false);
