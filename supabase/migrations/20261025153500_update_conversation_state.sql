-- Atomically replace a session-owned conversation's bounded state.
-- Used by country-only availability refreshes: the caller supplies the expected
-- version and the complete replacement state, so stale writes cannot overwrite
-- a newer result or interfere with an active run lease.

create function public.update_conversation_state(
    p_session_id uuid,
    p_conversation_id uuid,
    p_expected_version integer,
    p_state jsonb
)
returns table (
    conversation_id uuid,
    version integer,
    updated_at timestamptz
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
#variable_conflict use_column
declare
    v_now timestamptz := now();
    v_version integer;
    v_active_run_id uuid;
    v_active_expires timestamptz;
    v_updated_at timestamptz;
begin
    if p_session_id is null or p_conversation_id is null then
        raise exception 'ids_required' using errcode = '22023';
    end if;
    if p_expected_version is null or p_expected_version < 1 then
        raise exception 'invalid_expected_version' using errcode = '22023';
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

    select c.version, c.active_run_id, c.lease_expires_at
    into v_version, v_active_run_id, v_active_expires
    from public.conversations c
    where c.id = p_conversation_id
      and c.session_id = p_session_id
    for update;
    if not found then
        raise exception 'conversation_not_found' using errcode = '28000';
    end if;

    if v_version <> p_expected_version then
        raise exception 'version_conflict' using errcode = '55P03';
    end if;

    if v_active_run_id is not null and v_active_expires > v_now then
        raise exception 'run_in_progress' using errcode = '55P03';
    end if;

    -- An expired lease no longer owns the conversation. Close the abandoned
    -- run before clearing the lease so a later replay cannot finalize it.
    if v_active_run_id is not null then
        update public.runs r
        set status = 'failed',
            error_code = 'lease_expired',
            completed_at = v_now,
            updated_at = v_now
        where r.id = v_active_run_id
          and r.status = 'running';
    end if;

    update public.conversations c
    set state = p_state,
        version = c.version + 1,
        active_run_id = null,
        active_run_lease = null,
        lease_expires_at = null,
        updated_at = v_now
    where c.id = p_conversation_id
    returning c.version, c.updated_at into v_version, v_updated_at;

    return query select p_conversation_id, v_version, v_updated_at;
end;
$$;

comment on function public.update_conversation_state(uuid, uuid, integer, jsonb) is
    'Replaces bounded state only when the conversation version and lease allow it.';

revoke all on function public.update_conversation_state(uuid, uuid, integer, jsonb)
    from public, anon, authenticated, service_role;
grant execute on function public.update_conversation_state(uuid, uuid, integer, jsonb)
    to service_role;

notify pgrst, 'reload schema';
