-- Resolve the OUT parameter/table column name collision in the auth limiter.
-- The initial migration was applied through Supabase MCP and is recorded as
-- version 20261025111117; keep this repair as a separate forward migration.

create or replace function public.consume_auth_attempt(
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
#variable_conflict use_column
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

revoke all on function public.consume_auth_attempt(text, integer)
    from public, anon, authenticated, service_role;
grant execute on function public.consume_auth_attempt(text, integer) to service_role;
