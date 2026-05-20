-- Helper: tenant_id from JWT claim
create or replace function public.current_tenant_id()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'tenant_id','')::uuid
$$;

-- Enable RLS on every tenant-scoped table
do $$
declare t text;
begin
  foreach t in array array[
    'tenants','users','leads','campaigns','calls','call_events',
    'transcripts','lead_scores','handoffs','dnc_list'
  ] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- tenants: only own tenant
create policy tenant_self_read   on public.tenants for select
  using (id = public.current_tenant_id());
create policy tenant_self_update on public.tenants for update
  using (id = public.current_tenant_id());

-- users: visible only within same tenant
create policy users_same_tenant_read on public.users for select
  using (tenant_id = public.current_tenant_id());

-- Generic tenant-scoped policies for the rest
do $$
declare t text;
begin
  foreach t in array array[
    'leads','campaigns','calls','transcripts','lead_scores',
    'handoffs','dnc_list'
  ] loop
    execute format($f$
      create policy %1$I_tenant_read   on public.%1$I for select
        using (tenant_id = public.current_tenant_id());
      create policy %1$I_tenant_insert on public.%1$I for insert
        with check (tenant_id = public.current_tenant_id());
      create policy %1$I_tenant_update on public.%1$I for update
        using (tenant_id = public.current_tenant_id());
      create policy %1$I_tenant_delete on public.%1$I for delete
        using (tenant_id = public.current_tenant_id());
    $f$, t);
  end loop;
end $$;

-- call_events: scope through calls
create policy call_events_via_call_read on public.call_events for select
  using (exists (select 1 from public.calls c
                 where c.id = call_events.call_id
                   and c.tenant_id = public.current_tenant_id()));
create policy call_events_via_call_insert on public.call_events for insert
  with check (exists (select 1 from public.calls c
                      where c.id = call_events.call_id
                        and c.tenant_id = public.current_tenant_id()));
