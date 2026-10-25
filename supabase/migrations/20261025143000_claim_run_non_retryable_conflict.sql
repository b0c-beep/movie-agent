-- Earlier PostgREST versions retried SQLSTATE 40001 serialization failures.
-- claim_run intentionally used 40001 for version and active-lease conflicts,
-- so an upstream retry loop could repeatedly hit the same conflict and time
-- out. Keep the proven implementation and expose a thin wrapper that turns
-- only those intentional claim conflicts into 55P03 (lock_not_available),
-- which is non-retryable. Genuine 40001 errors pass through unchanged.

alter function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) rename to claim_run_internal;

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
#variable_conflict use_column
declare
    v_message text;
    v_detail text;
    v_hint text;
begin
    return query select *
    from public.claim_run_internal(
        p_session_id,
        p_conversation_id,
        p_client_request_id,
        p_expected_version,
        p_input,
        p_lease_seconds,
        p_session_hourly_limit,
        p_global_hourly_limit
    );
exception
    when sqlstate '40001' then
        get stacked diagnostics
            v_message = message_text,
            v_detail = pg_exception_detail,
            v_hint = pg_exception_hint;

        -- Only the two conflicts raised deliberately by claim_run are
        -- translated. Other 40001 failures retain their original SQLSTATE.
        if v_message in ('version_conflict', 'run_in_progress') then
            raise exception '%', v_message
                using errcode = '55P03',
                      detail = v_detail,
                      hint = v_hint;
        end if;

        raise;
end;
$$;

comment on function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) is
    'Claims or replays a run; intentional claim conflicts raise 55P03.';

-- The renamed implementation remains SECURITY DEFINER. Remove its execute
-- grants so callers cannot bypass the wrapper; the wrapper runs as the same
-- owner and can still invoke it.
revoke all on function public.claim_run_internal(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) from public, anon, authenticated, service_role;

revoke all on function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) from public, anon, authenticated, service_role;
grant execute on function public.claim_run(
    uuid, uuid, text, integer, jsonb, integer, integer, integer
) to service_role;

notify pgrst, 'reload schema';
