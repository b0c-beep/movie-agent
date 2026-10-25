-- Disambiguate PL/pgSQL OUT parameter names from target table columns.

create or replace function public.create_conversation(
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
#variable_conflict use_column
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

create or replace function public.claim_run(
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
#variable_conflict use_column
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

revoke all on function public.create_conversation(uuid, jsonb) from public, anon, authenticated, service_role;
grant execute on function public.create_conversation(uuid, jsonb) to service_role;
revoke all on function public.claim_run(uuid, uuid, text, integer, jsonb, integer, integer, integer) from public, anon, authenticated, service_role;
grant execute on function public.claim_run(uuid, uuid, text, integer, jsonb, integer, integer, integer) to service_role;
