-- Regression test for atomic, versioned conversation state replacement.
-- `supabase test db` runs this in a disposable local database.
do $test$
declare
    v_session_id uuid;
    v_other_session_id uuid;
    v_conversation_id uuid;
    v_db_state jsonb;
    v_db_version integer;
    v_version integer;
    v_updated_at timestamptz;
    v_run_id uuid;
    v_lease_token uuid;
    v_run_status text;
    v_run_error text;
    v_message text;
begin
    insert into public.sessions (expires_at)
    values (now() + interval '1 hour')
    returning id into v_session_id;

    insert into public.sessions (expires_at)
    values (now() + interval '1 hour')
    returning id into v_other_session_id;

    select c.conversation_id into v_conversation_id
    from public.create_conversation(v_session_id) as c;
    if v_conversation_id is null then
        raise exception 'conversation creation failed';
    end if;

    select u.version, u.updated_at
    into v_version, v_updated_at
    from public.update_conversation_state(
        v_session_id,
        v_conversation_id,
        1,
        '{"intent":{"country":"DE"},"last_result":{"recommendations":[]}}'::jsonb
    ) as u;
    if v_version <> 2 or v_updated_at is null then
        raise exception 'update should increment version to 2';
    end if;

    select c.state, c.version
    into v_db_state, v_db_version
    from public.conversations c
    where c.id = v_conversation_id;
    if v_db_version <> 2 or v_db_state -> 'intent' ->> 'country' <> 'DE' then
        raise exception 'conversation state was not persisted';
    end if;

    begin
        perform * from public.update_conversation_state(
            v_session_id, v_conversation_id, 1, '{}'::jsonb
        );
        raise exception 'expected version conflict';
    exception
        when sqlstate '55P03' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'version_conflict' then
                raise exception 'unexpected version error message: %', v_message;
            end if;
    end;

    begin
        perform * from public.update_conversation_state(
            v_session_id, v_conversation_id, 0, '{}'::jsonb
        );
        raise exception 'expected invalid version';
    exception
        when sqlstate '22023' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'invalid_expected_version' then
                raise exception 'unexpected version validation message: %', v_message;
            end if;
    end;

    begin
        perform * from public.update_conversation_state(
            v_session_id, v_conversation_id, 2, '[]'::jsonb
        );
        raise exception 'expected invalid state';
    exception
        when sqlstate '22023' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'invalid_state' then
                raise exception 'unexpected state validation message: %', v_message;
            end if;
    end;

    begin
        perform * from public.update_conversation_state(
            v_other_session_id, v_conversation_id, 2, '{}'::jsonb
        );
        raise exception 'expected owner conflict';
    exception
        when sqlstate '28000' then
            get stacked diagnostics v_message = message_text;
            if v_message <> 'conversation_not_found' then
                raise exception 'unexpected owner error message: %', v_message;
            end if;
    end;

    select r.run_id, r.lease_token
    into v_run_id, v_lease_token
    from public.claim_run(
        v_session_id, v_conversation_id, 'state-active-run', 2
    ) as r;

    begin
        perform * from public.update_conversation_state(
            v_session_id, v_conversation_id, 2, '{}'::jsonb
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
        '{"intent":{"country":"DE"}}'::jsonb,
        '{}'::jsonb,
        '[]'::jsonb
    );

    select u.version into v_version
    from public.update_conversation_state(
        v_session_id, v_conversation_id, 3, '{"intent":{"country":"FR"}}'::jsonb
    ) as u;
    if v_version <> 4 then
        raise exception 'update after completion should increment version to 4';
    end if;

    select r.run_id, r.lease_token
    into v_run_id, v_lease_token
    from public.claim_run(
        v_session_id, v_conversation_id, 'state-expired-run', 4
    ) as r;

    update public.conversations c
    set lease_expires_at = now() - interval '1 second'
    where c.id = v_conversation_id;

    select u.version into v_version
    from public.update_conversation_state(
        v_session_id, v_conversation_id, 4, '{"intent":{"country":"IT"}}'::jsonb
    ) as u;
    if v_version <> 5 then
        raise exception 'expired lease should not block the update';
    end if;

    select r.status, r.error_code
    into v_run_status, v_run_error
    from public.runs r
    where r.id = v_run_id;
    if v_run_status <> 'failed' or v_run_error <> 'lease_expired' then
        raise exception 'expired run should be closed as lease_expired';
    end if;

    if not has_function_privilege(
        'service_role',
        'public.update_conversation_state(uuid,uuid,integer,jsonb)',
        'execute'
    ) then
        raise exception 'service_role should execute the update RPC';
    end if;
    if has_function_privilege(
        'anon',
        'public.update_conversation_state(uuid,uuid,integer,jsonb)',
        'execute'
    ) then
        raise exception 'anon must not execute the update RPC';
    end if;

    delete from public.usage_buckets
    where scope = 'session'
      and bucket_key in (v_session_id::text, v_other_session_id::text);
    delete from public.sessions
    where id in (v_session_id, v_other_session_id);
end;
$test$;
