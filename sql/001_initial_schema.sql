-- Despacho Contable Fiscal SL — bot schema inicial
-- Ejecutar en el Supabase SQL Editor del proyecto dedicado al despacho.

create extension if not exists "uuid-ossp";

-- ==========================================================
-- customers — cada prospecto que escribe al bot
-- ==========================================================
create table if not exists customers (
  id uuid primary key default uuid_generate_v4(),
  manychat_user_id text unique not null,
  phone text,
  first_name text,
  last_name text,
  email text,
  stage text not null default 'lead_new',
    -- lead_new, lead_qualified,
    -- escalated_interesado, escalated_regularizacion, escalated_seguimiento,
    -- escalated_no_interesado, escalated_cliente,
    -- blocked_rude, blocked_crisis
  notes text,
  blocked_until timestamptz,
  block_reason text,
    -- rude | crisis | manual | spam
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_customers_stage on customers(stage);
create index if not exists idx_customers_phone on customers(phone);
create index if not exists idx_customers_blocked_until on customers(blocked_until)
  where blocked_until is not null;

create or replace function trigger_set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists customers_updated_at on customers;
create trigger customers_updated_at
  before update on customers
  for each row execute function trigger_set_updated_at();

-- ==========================================================
-- messages — historial de conversación (memoria del bot)
-- ==========================================================
create table if not exists messages (
  id uuid primary key default uuid_generate_v4(),
  customer_id uuid not null references customers(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_messages_customer_created
  on messages(customer_id, created_at);

-- ==========================================================
-- escalations — cuando el bot clasifica al prospecto
-- ==========================================================
create table if not exists escalations (
  id uuid primary key default uuid_generate_v4(),
  customer_id uuid not null references customers(id) on delete cascade,
  reason text not null,
    -- 'interesado' | 'regularizacion' | 'seguimiento' | 'no_interesado' | 'cliente'
  resolved boolean not null default false,
  resolved_at timestamptz,
  resolved_by text,
    -- nombre del ejecutivo del despacho que atendió
  created_at timestamptz not null default now()
);

create index if not exists idx_escalations_customer on escalations(customer_id);
create index if not exists idx_escalations_unresolved
  on escalations(resolved) where resolved = false;

-- ==========================================================
-- events — timeline para analítica y auditoría
-- ==========================================================
create table if not exists events (
  id uuid primary key default uuid_generate_v4(),
  customer_id uuid references customers(id) on delete cascade,
  event_type text not null,
  payload jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_events_customer_created
  on events(customer_id, created_at);
create index if not exists idx_events_type_created
  on events(event_type, created_at);

-- ==========================================================
-- RLS — habilitada (regla de oro: el bot usa service_role que la ignora,
-- pero esto cierra el acceso anónimo a datos fiscales sensibles).
-- ==========================================================
alter table customers enable row level security;
alter table messages enable row level security;
alter table escalations enable row level security;
alter table events enable row level security;

-- Sin políticas explícitas: solo service_role puede leer/escribir, todo lo
-- demás (anon, authenticated) queda bloqueado por default. Si alguna vez
-- se necesita exponer una vista al frontend, se agregan policies aquí.

-- ==========================================================
-- Comentarios de documentación
-- ==========================================================
comment on table customers is 'Prospectos del Despacho Contable Fiscal SL que llegan por WhatsApp/IG/Messenger';
comment on column customers.stage is 'Etapa actual del prospecto en el funnel del despacho';
comment on table messages is 'Historial completo de mensajes user/assistant — memoria conversacional del bot';
comment on table escalations is 'Eventos de clasificación: cuando el bot pasa el prospecto a un ejecutivo humano';
comment on table events is 'Timeline de eventos para analítica y auditoría';
