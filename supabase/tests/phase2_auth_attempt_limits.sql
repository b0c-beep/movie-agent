-- Regression test for the auth limiter's OUT-column/target-column name clash.
-- `supabase test db` executes this in a disposable local database; cleanup also
-- keeps this safe to run manually against a development project.
do $test$
declare
    v_bucket_key text := md5(clock_timestamp()::text) || md5(random()::text);
    v_allowed boolean;
    v_request_count integer;
begin
    select attempt.allowed, attempt.request_count
    into v_allowed, v_request_count
    from public.consume_auth_attempt(v_bucket_key, 1) as attempt;

    if not v_allowed or v_request_count <> 1 then
        raise exception 'first auth attempt should be allowed with count 1';
    end if;

    select attempt.allowed, attempt.request_count
    into v_allowed, v_request_count
    from public.consume_auth_attempt(v_bucket_key, 1) as attempt;

    if v_allowed or v_request_count <> 1 then
        raise exception 'second auth attempt should be denied at count 1';
    end if;

    delete from public.usage_buckets
    where scope = 'auth' and bucket_key = v_bucket_key;
end;
$test$;
