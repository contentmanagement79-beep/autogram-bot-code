-- ============================================================
--  AUTOGRAM — PHASE 3B SCHEMA (bot engine)
--  Run in Supabase → SQL Editor (after phase3-schema.sql).
-- ============================================================

-- Per-customer conversation memory (auto-expired after ~7 days) -------------
create table if not exists public.conversations (
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,  -- the tenant (owner)
  customer_id bigint not null,       -- telegram id of the customer chatting
  role        text not null,         -- 'user' (customer) | 'assistant'
  content     text not null,
  created_at  timestamptz not null default now()
);
create index if not exists idx_conv_lookup on public.conversations (user_id, customer_id, created_at desc);

-- Per-customer pause (human take-over) --------------------------------------
create table if not exists public.chat_states (
  user_id     uuid not null references auth.users (id) on delete cascade,
  customer_id bigint not null,
  paused      boolean not null default false,
  updated_at  timestamptz not null default now(),
  primary key (user_id, customer_id)
);

-- RLS: these are written by the engine (service role, bypasses RLS) but the
-- dashboard should only read its own rows.
alter table public.conversations enable row level security;
alter table public.chat_states  enable row level security;
create policy "own conversations" on public.conversations for select
  using (auth.uid() = user_id);
create policy "own chat_states" on public.chat_states for select
  using (auth.uid() = user_id);
