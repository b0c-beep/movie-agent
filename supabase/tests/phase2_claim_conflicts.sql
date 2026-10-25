-- Regression test for non-retryable claim conflicts exposed through PostgREST.
-- `supabase test db` runs this in a disposable local database.
do $test$
declare
    v_session_id uuid;
    v_conversation_id uuid;
    v_run_id uuid;
    v_lease_token uuid;
    v_replay_run_id uuid;
    v_replay_status text;
    v_replay_lease uuid;
    v_replayed boolean;
    v_message text;
begin
    insert into public.sessions (expires_at)
    values (now() + interval '1 hour')
    returning id into v_session_id;

    select c.conversation_id into v_conversation_id
    from public.create_conversation(v_session_id) as c;

    select r.run_id, r.lease_token, r.status, r.replayed
    into v_run_id, v_lease_token, v_replay_status, v_replayed
    from public.claim_run(v_session_id, v_conversation_id, 'claim-first', 1) as r;
    if v_run_id is null or v_lease_token is null
       or v_replay_status <> 'running' or v_replayed then
        raise exception 'first claim should return a run and lease';
    end if;

    select r.run_id, r.lease_token, r.status, r.replayed
    into v_replay_run_id, v_replay_lease, v_replay_status, v_replayed
    from public.claim_run(v_session_id, v_conversation_id, 'claim-first', 1) as r;
    if v_replay_run_id <> v_run_id or v_replay_lease is not null
       or v_replay_status <> 'running' or not v_replayed then
        raise exception 'same-id active replay semantics changed';
    end if;

    begin
        perform * from public.claim_run(
            v_session_id, v_conversation_id, 'claim-second', 1
        );
        raise exception 'expected active-run conflict';
    exception
        when sqlstate '55P03' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'run_in_progress' then
                raise exception 'unexpected active-run error message: %', v_message;
            end if;
    end;

    perform * from public.finalize_run(
        v_session_id,
        v_run_id,
        v_lease_token,
        'completed',
        '{}'::jsonb,
        '{}'::jsonb,
        '[]'::jsonb
    );

    begin
        perform * from public.claim_run(
            v_session_id, v_conversation_id, 'claim-stale-version', 1
        );
        raise exception 'expected version conflict';
    exception
        when sqlstate '55P03' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'version_conflict' then
                raise exception 'unexpected version error message: %', v_message;
            end if;
    end;

    if not has_function_privilege(
        'service_role',
        'public.claim_run(uuid,uuid,text,integer,jsonb,integer,integer,integer)',
        'execute'
    ) then
        raise exception 'service_role should execute the public wrapper';
    end if;
    if has_function_privilege(
        'service_role',
        'public.claim_run_internal(uuid,uuid,text,integer,jsonb,integer,integer,integer)',
        'execute'
    ) then
        raise exception 'service_role must not bypass the public wrapper';
    end if;
    if has_function_privilege(
        'anon',
        'public.claim_run(uuid,uuid,text,integer,jsonb,integer,integer,integer)',
        'execute'
    ) then
        raise exception 'anon must not execute the public wrapper';
    end if;

    delete from public.usage_buckets
    where scope = 'session' and bucket_key = v_session_id::text;
    delete from public.sessions where id = v_session_id;
end;
$test$;
