-- Phase 2 initial persistence schema.
-- Browser clients never access these tables. Only service_role (which bypasses
-- RLS in Supabase) and fixed-search_path SECURITY DEFINER functions do.

create table public.sessions (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    constraint sessions_expiry_check check (expires_at > created_at),
    constraint sessions_revoked_check check (revoked_at is null or revoked_at >= created_at)
);

create index sessions_active_expiry_idx
    on public.sessions (expires_at)
    where revoked_at is null;

create table public.conversations (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references public.sessions (id) on delete cascade,
    state jsonb not null default '{}'::jsonb,
    version integer not null default 1,
    active_run_id uuid,
    active_run_lease uuid,
    lease_expires_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint conversations_state_check
        check (jsonb_typeof(state) = 'object' and octet_length(state::text) <= 65536),
    constraint conversations_version_check check (version >= 1),
    constraint conversations_lease_check check (
        (active_run_id is null and active_run_lease is null and lease_expires_at is null)
        or (active_run_id is not null and active_run_lease is not null and lease_expires_at is not null)
    )
);

comment on column public.conversations.active_run_id is
    'Lease pointer to runs.id; no FK to avoid a circular dependency.';

create index conversations_session_idx
    on public.conversations (session_id, created_at desc);
create index conversations_active_lease_idx
    on public.conversations (lease_expires_at)
    where active_run_id is not null;

create table public.runs (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations (id) on delete cascade,
    client_request_id text not null,
    status text not null default 'running',
    base_version integer not null,
    lease_token uuid not null,
    input jsonb not null default '{}'::jsonb,
    result jsonb,
    trace jsonb,
    error_code text,
    usage jsonb,
    model text,
    created_at timestamptz not null default now(),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    updated_at timestamptz not null default now(),
    constraint runs_status_check check (
        status in ('running', 'completed', 'needs_clarification', 'failed', 'cancelled')
    ),
    constraint runs_client_request_id_check
        check (client_request_id ~ '^[A-Za-z0-9._:-]{1,128}$'),
    constraint runs_base_version_check check (base_version >= 1),
    constraint runs_input_check
        check (jsonb_typeof(input) = 'object' and octet_length(input::text) <= 16384),
    constraint runs_result_check check (
        result is null
        or (jsonb_typeof(result) in ('object', 'array') and octet_length(result::text) <= 262144)
    ),
    constraint runs_trace_check check (
        trace is null
        or (jsonb_typeof(trace) = 'array' and octet_length(trace::text) <= 131072)
    ),
    constraint runs_error_code_check
        check (error_code is null or error_code ~ '^[a-z0-9_]{1,64}$'),
    constraint runs_usage_check check (
        usage is null
        or (jsonb_typeof(usage) = 'object' and octet_length(usage::text) <= 8192)
    ),
    constraint runs_model_check
        check (model is null or model ~ '^[A-Za-z0-9._:/-]{1,128}$'),
    constraint runs_completed_at_check
        check ((status = 'running') = (completed_at is null)),
    unique (conversation_id, client_request_id)
);

create index runs_conversation_created_idx
    on public.runs (conversation_id, created_at desc);
create index runs_running_idx
    on public.runs (conversation_id)
    where status = 'running';
create index runs_created_at_idx on public.runs (created_at);

create table public.usage_buckets (
    id uuid primary key default gen_random_uuid(),
    scope text not null,
    bucket_key text not null,
    window_start timestamptz not null,
    request_count integer not null default 0,
    updated_at timestamptz not null default now(),
    constraint usage_buckets_scope_check check (scope in ('session', 'global', 'auth')),
    constraint usage_buckets_key_check check (char_length(bucket_key) between 1 and 128),
    constraint usage_buckets_global_key_check check (
        (scope = 'global' and bucket_key = 'global')
        or (scope in ('session', 'auth') and bucket_key <> 'global')
    ),
    constraint usage_buckets_auth_key_check check (
        scope <> 'auth' or bucket_key ~ '^[0-9a-f]{64}$'
    ),
    constraint usage_buckets_count_check check (request_count >= 0),
    unique (scope, bucket_key, window_start)
);

comment on table public.usage_buckets is
    'UTC-hour request counters written only by fixed RPCs.';

create index usage_buckets_window_idx on public.usage_buckets (window_start);

alter table public.sessions enable row level security;
alter table public.conversations enable row level security;
alter table public.runs enable row level security;
alter table public.usage_buckets enable row level security;

-- No RLS policies: anonymous/authenticated access is denied. service_role
-- bypasses RLS in Supabase and receives only the direct access it needs.
revoke all on table public.sessions, public.conversations, public.runs, public.usage_buckets
    from public, anon, authenticated, service_role;
grant select, insert, update, delete on table public.sessions to service_role;
grant select, update on table public.conversations to service_role;
grant select on table public.runs to service_role;
grant select on table public.usage_buckets to service_role;

create function public.create_conversation(
    p_session_id uuid,
    p_state jsonb default '{}'::jsonb
)
returns table (
    conversation_id uuid,
    version integer,
    created_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
    v_now timestamptz := now();
    v_id uuid;
    v_version integer;
    v_created_at timestamptz;
begin
    if p_session_id is null then
        raise exception 'session_id_required' using errcode = '22023';
    end if;
    if p_state is null
       or jsonb_typeof(p_state) <> 'object'
       or octet_length(p_state::text) > 65536 then
        raise exception 'invalid_state' using errcode = '22023';
    end if;

    perform 1
    from public.sessions s
    where s.id = p_session_id
      and s.revoked_at is null
      and s.expires_at > v_now
    for share;
    if not found then
        raise exception 'session_invalid' using errcode = '28000';
    end if;

    insert into public.conversations (session_id, state, version, created_at, updated_at)
    values (p_session_id, p_state, 1, v_now, v_now)
    returning id, version, created_at
    into v_id, v_version, v_created_at;

    return query select v_id, v_version, v_created_at;
end;
$$;

create function public.claim_run(
    p_session_id uuid,
    p_conversation_id uuid,
    p_client_request_id text,
    p_expected_version integer,
    p_input jsonb default '{}'::jsonb,
    p_lease_seconds integer default 120,
    p_session_hourly_limit integer default 60,
    p_global_hourly_limit integer default 600
)
returns table (
    run_id uuid,
    status text,
    replayed boolean,
    lease_token uuid,
    lease_expires_at timestamptz,
    base_version integer,
    result jsonb,
    trace jsonb,
    error_code text,
    usage jsonb,
    created_at timestamptz,
    completed_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
    v_now timestamptz := now();
    v_window timestamptz :=
        date_trunc('hour', v_now at time zone 'UTC') at time zone 'UTC';
    v_run_id uuid;
    v_lease_token uuid;
    v_lease_expires_at timestamptz;
    v_conv_version integer;
    v_active_run_id uuid;
    v_active_lease uuid;
    v_active_expires timestamptz;
    v_count integer;
    v_existing public.runs%rowtype;
begin
    if p_session_id is null or p_conversation_id is null then
        raise exception 'ids_required' using errcode = '22023';
    end if;
    if p_client_request_id is null
       or p_client_request_id !~ '^[A-Za-z0-9._:-]{1,128}$' then
        raise exception 'invalid_client_request_id' using errcode = '22023';
    end if;
    if p_expected_version is null or p_expected_version < 1 then
        raise exception 'invalid_expected_version' using errcode = '22023';
    end if;
    if p_input is null
       or jsonb_typeof(p_input) <> 'object'
       or octet_length(p_input::text) > 16384 then
        raise exception 'invalid_input' using errcode = '22023';
    end if;
    if p_lease_seconds is null or p_lease_seconds < 10 or p_lease_seconds > 300 then
        raise exception 'invalid_lease_seconds' using errcode = '22023';
    end if;
    if p_session_hourly_limit is null
       or p_session_hourly_limit < 1
       or p_session_hourly_limit > 100000 then
        raise exception 'invalid_session_limit' using errcode = '22023';
    end if;
    if p_global_hourly_limit is null
       or p_global_hourly_limit < 1
       or p_global_hourly_limit > 100000 then
        raise exception 'invalid_global_limit' using errcode = '22023';
    end if;

    perform 1
    from public.sessions s
    where s.id = p_session_id
      and s.revoked_at is null
      and s.expires_at > v_now
    for share;
    if not found then
        raise exception 'session_invalid' using errcode = '28000';
    end if;

    select c.version, c.active_run_id, c.active_run_lease, c.lease_expires_at
    into v_conv_version, v_active_run_id, v_active_lease, v_active_expires
    from public.conversations c
    where c.id = p_conversation_id
      and c.session_id = p_session_id
    for update;
    if not found then
        raise exception 'conversation_not_found' using errcode = '28000';
    end if;

    select r.*
    into v_existing
    from public.runs r
    where r.conversation_id = p_conversation_id
      and r.client_request_id = p_client_request_id;
    if found then
        -- Abandoned runs fail on replay; a live lease is reused as-is.
        if v_existing.status = 'running'
           and (v_active_run_id is distinct from v_existing.id
                or v_active_lease is distinct from v_existing.lease_token
                or v_active_expires is null
                or v_active_expires <= v_now) then
            update public.runs r
            set status = 'failed',
                error_code = 'lease_expired',
                completed_at = v_now,
                updated_at = v_now
            where r.id = v_existing.id
              and r.status = 'running';

            if v_active_run_id = v_existing.id then
                update public.conversations c
                set active_run_id = null,
                    active_run_lease = null,
                    lease_expires_at = null,
                    updated_at = v_now
                where c.id = p_conversation_id;
            end if;

            v_existing.status := 'failed';
            v_existing.error_code := 'lease_expired';
            v_existing.completed_at := v_now;
        end if;

        return query select
            v_existing.id,
            v_existing.status,
            true,
            null::uuid,
            null::timestamptz,
            v_existing.base_version,
            v_existing.result,
            v_existing.trace,
            v_existing.error_code,
            v_existing.usage,
            v_existing.created_at,
            v_existing.completed_at;
        return;
    end if;

    if v_conv_version <> p_expected_version then
        raise exception 'version_conflict' using errcode = '40001';
    end if;

    if v_active_run_id is not null then
        if v_active_expires > v_now then
            raise exception 'run_in_progress' using errcode = '40001';
        end if;
        update public.runs r
        set status = 'failed',
            error_code = 'lease_expired',
            completed_at = v_now,
            updated_at = v_now
        where r.id = v_active_run_id
          and r.status = 'running';
    end if;

    insert into public.usage_buckets as ub
        (scope, bucket_key, window_start, request_count, updated_at)
    values
        ('session', p_session_id::text, v_window, 1, v_now)
    on conflict (scope, bucket_key, window_start) do update
    set request_count = ub.request_count + 1,
        updated_at = excluded.updated_at
    where ub.request_count < p_session_hourly_limit
    returning request_count into v_count;
    if v_count is null then
        raise exception 'session_rate_limited' using errcode = 'P0001';
    end if;

    insert into public.usage_buckets as ub
        (scope, bucket_key, window_start, request_count, updated_at)
    values
        ('global', 'global', v_window, 1, v_now)
    on conflict (scope, bucket_key, window_start) do update
    set request_count = ub.request_count + 1,
        updated_at = excluded.updated_at
    where ub.request_count < p_global_hourly_limit
    returning request_count into v_count;
    if v_count is null then
        raise exception 'global_rate_limited' using errcode = 'P0001';
    end if;

    v_run_id := gen_random_uuid();
    v_lease_token := gen_random_uuid();
    v_lease_expires_at := v_now + make_interval(secs => p_lease_seconds);

    insert into public.runs (
        id, conversation_id, client_request_id, status, base_version,
        lease_token, input, created_at, started_at, updated_at
    ) values (
        v_run_id, p_conversation_id, p_client_request_id, 'running', p_expected_version,
        v_lease_token, p_input, v_now, v_now, v_now
    );

    update public.conversations c
    set active_run_id = v_run_id,
        active_run_lease = v_lease_token,
        lease_expires_at = v_lease_expires_at,
        updated_at = v_now
    where c.id = p_conversation_id;

    return query select
        v_run_id,
        'running'::text,
        false,
        v_lease_token,
        v_lease_expires_at,
        p_expected_version,
        null::jsonb,
        null::jsonb,
        null::text,
        null::jsonb,
        v_now,
        null::timestamptz;
end;
$$;

create function public.finalize_run(
    p_session_id uuid,
    p_run_id uuid,
    p_lease_token uuid,
    p_status text,
    p_state jsonb default null,
    p_result jsonb default null,
    p_trace jsonb default null,
    p_error_code text default null,
    p_usage jsonb default null,
    p_model text default null
)
returns table (
    run_id uuid,
    conversation_id uuid,
    version integer,
    status text,
    completed_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
    v_now timestamptz := now();
    v_conversation_id uuid;
    v_version integer;
    v_active_run_id uuid;
    v_active_lease uuid;
    v_status text;
    v_base_version integer;
    v_completed_at timestamptz;
begin
    if p_session_id is null or p_run_id is null or p_lease_token is null then
        raise exception 'ids_required' using errcode = '22023';
    end if;
    if p_status is null
       or p_status not in ('completed', 'needs_clarification', 'failed', 'cancelled') then
        raise exception 'invalid_status' using errcode = '22023';
    end if;
    if p_status in ('completed', 'needs_clarification') then
        if p_state is null
           or jsonb_typeof(p_state) <> 'object'
           or octet_length(p_state::text) > 65536 then
            raise exception 'invalid_state' using errcode = '22023';
        end if;
    elsif p_state is not null then
        raise exception 'state_not_allowed' using errcode = '22023';
    end if;
    if p_result is not null and (
        jsonb_typeof(p_result) not in ('object', 'array')
        or octet_length(p_result::text) > 262144
    ) then
        raise exception 'invalid_result' using errcode = '22023';
    end if;
    if p_trace is not null and (
        jsonb_typeof(p_trace) <> 'array'
        or octet_length(p_trace::text) > 131072
    ) then
        raise exception 'invalid_trace' using errcode = '22023';
    end if;
    if p_error_code is not null and p_error_code !~ '^[a-z0-9_]{1,64}$' then
        raise exception 'invalid_error_code' using errcode = '22023';
    end if;
    if p_status = 'failed' and p_error_code is null then
        raise exception 'error_code_required' using errcode = '22023';
    end if;
    if p_usage is not null and (
        jsonb_typeof(p_usage) <> 'object'
        or octet_length(p_usage::text) > 8192
    ) then
        raise exception 'invalid_usage' using errcode = '22023';
    end if;
    if p_model is not null and p_model !~ '^[A-Za-z0-9._:/-]{1,128}$' then
        raise exception 'invalid_model' using errcode = '22023';
    end if;

    -- Lock order is always conversation then run, matching claim_run.
    select c.id, c.version, c.active_run_id, c.active_run_lease
    into v_conversation_id, v_version, v_active_run_id, v_active_lease
    from public.conversations c
    join public.runs r on r.conversation_id = c.id
    where r.id = p_run_id
      and c.session_id = p_session_id
    for update of c;
    if not found then
        raise exception 'run_not_found' using errcode = '28000';
    end if;

    select r.status, r.base_version, r.completed_at
    into v_status, v_base_version, v_completed_at
    from public.runs r
    where r.id = p_run_id
      and r.conversation_id = v_conversation_id
    for update;
    if not found then
        raise exception 'run_not_found' using errcode = '28000';
    end if;

    if v_status <> 'running' then
        return query select
            p_run_id, v_conversation_id, v_version, v_status, v_completed_at;
        return;
    end if;

    if v_active_run_id is distinct from p_run_id
       or v_active_lease is distinct from p_lease_token then
        raise exception 'lease_mismatch' using errcode = '40001';
    end if;
    if v_version <> v_base_version then
        raise exception 'version_conflict' using errcode = '40001';
    end if;

    update public.runs r
    set status = p_status,
        result = p_result,
        trace = p_trace,
        error_code = p_error_code,
        usage = p_usage,
        model = p_model,
        completed_at = v_now,
        updated_at = v_now
    where r.id = p_run_id;

    if p_status in ('completed', 'needs_clarification') then
        update public.conversations c
        set state = p_state,
            version = c.version + 1,
            active_run_id = null,
            active_run_lease = null,
            lease_expires_at = null,
            updated_at = v_now
        where c.id = v_conversation_id
        returning c.version into v_version;
    else
        update public.conversations c
        set active_run_id = null,
            active_run_lease = null,
            lease_expires_at = null,
            updated_at = v_now
        where c.id = v_conversation_id;
    end if;

    return query select p_run_id, v_conversation_id, v_version, p_status, v_now;
end;
$$;

create function public.consume_auth_attempt(
    p_bucket_key text,
    p_hourly_limit integer default 10
)
returns table (
    allowed boolean,
    request_count integer
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
    v_now timestamptz := now();
    v_window timestamptz :=
        date_trunc('hour', v_now at time zone 'UTC') at time zone 'UTC';
    v_count integer;
begin
    if p_bucket_key is null or p_bucket_key !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid_bucket_key' using errcode = '22023';
    end if;
    if p_hourly_limit is null
       or p_hourly_limit < 1
       or p_hourly_limit > 100000 then
        raise exception 'invalid_hourly_limit' using errcode = '22023';
    end if;

    insert into public.usage_buckets as ub
        (scope, bucket_key, window_start, request_count, updated_at)
    values
        ('auth', p_bucket_key, v_window, 1, v_now)
    on conflict (scope, bucket_key, window_start) do update
    set request_count = ub.request_count + 1,
        updated_at = excluded.updated_at
    where ub.request_count < p_hourly_limit
    returning request_count into v_count;

    if v_count is null then
        select ub.request_count
        into v_count
        from public.usage_buckets ub
        where ub.scope = 'auth'
          and ub.bucket_key = p_bucket_key
          and ub.window_start = v_window;
        return query select false, coalesce(v_count, p_hourly_limit);
        return;
    end if;

    return query select true, v_count;
end;
$$;

revoke all on function public.create_conversation(uuid, jsonb)
    from public, anon, authenticated, service_role;
revoke all on function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) from public, anon, authenticated, service_role;
revoke all on function public.finalize_run(
    uuid, uuid, uuid, text, jsonb, jsonb, jsonb, text, jsonb, text
) from public, anon, authenticated, service_role;
revoke all on function public.consume_auth_attempt(text, integer)
    from public, anon, authenticated, service_role;

grant execute on function public.create_conversation(uuid, jsonb) to service_role;
grant execute on function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) to service_role;
grant execute on function public.finalize_run(
    uuid, uuid, uuid, text, jsonb, jsonb, jsonb, text, jsonb, text
) to service_role;
grant execute on function public.consume_auth_attempt(text, integer) to service_role;
