-- Supabase Postgres schema for OJTTimer
-- Apply in Supabase SQL Editor (or via migrations) before deploying to Vercel.

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

-- For this app, PostgREST/RLS is not used directly (the Flask server talks to Postgres).
-- Keep RLS enabled if you want; just ensure your DATABASE_URL user can access these tables.
