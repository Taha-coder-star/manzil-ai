-- Run as the schema owner with CREATEROLE, never as the runtime login.
-- No passwords belong in migrations. Provision the login privately afterward.
DO $migration$
DECLARE
    target_schema TEXT := current_schema();
    app_role RECORD;
    table_name TEXT;
    tenant_column TEXT;
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'manzil_app') THEN
        CREATE ROLE manzil_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;

    SELECT * INTO STRICT app_role
    FROM pg_catalog.pg_roles WHERE rolname = 'manzil_app';

    IF NOT app_role.rolcanlogin OR app_role.rolsuper OR app_role.rolcreatedb
       OR app_role.rolcreaterole OR app_role.rolinherit
       OR app_role.rolreplication OR app_role.rolbypassrls
       OR EXISTS (SELECT FROM pg_catalog.pg_auth_members WHERE member = app_role.oid)
    THEN
        RAISE EXCEPTION 'manzil_app has unsafe attributes or role memberships';
    END IF;

    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace
        WHERE nspname = target_schema AND nspowner = app_role.oid
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_database
        WHERE datname = current_database() AND datdba = app_role.oid
    ) THEN
        RAISE EXCEPTION 'manzil_app must not own the application schema or database';
    END IF;

    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM manzil_app', target_schema);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO manzil_app', target_schema);

    EXECUTE format($function$
        CREATE FUNCTION %I.current_agency_id() RETURNS UUID
        LANGUAGE SQL STABLE SECURITY INVOKER
        SET search_path = pg_catalog
        AS 'SELECT NULLIF(pg_catalog.current_setting(''app.agency_id'', TRUE), '''')::UUID'
    $function$, target_schema);
    EXECUTE format(
        'REVOKE ALL ON FUNCTION %I.current_agency_id() FROM PUBLIC', target_schema
    );
    EXECUTE format(
        'GRANT EXECUTE ON FUNCTION %I.current_agency_id() TO manzil_app', target_schema
    );

    FOREACH table_name IN ARRAY ARRAY[
        'agency', 'staff', 'tenant_secret', 'inbound_event', 'job',
        'conversation', 'message', 'outbound_message', 'usage_event'
    ] LOOP
        IF EXISTS (
            SELECT FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = target_schema AND c.relname = table_name
              AND c.relowner = app_role.oid
        ) THEN
            RAISE EXCEPTION 'manzil_app must not own application tables';
        END IF;
        -- Existing permissive policies could silently widen access via OR.
        IF EXISTS (
            SELECT FROM pg_catalog.pg_policies
            WHERE schemaname = target_schema AND tablename = table_name
        ) THEN
            RAISE EXCEPTION 'Unexpected existing application RLS policies';
        END IF;

        tenant_column := CASE WHEN table_name = 'agency' THEN 'id' ELSE 'agency_id' END;
        EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM PUBLIC, manzil_app',
                       target_schema, table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO manzil_app',
                       target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I.%I FOR ALL TO manzil_app '
            'USING (%I = %I.current_agency_id()) '
            'WITH CHECK (%I = %I.current_agency_id())',
            target_schema, table_name, tenant_column, target_schema,
            tenant_column, target_schema
        );
    END LOOP;

    EXECUTE format('REVOKE ALL ON TABLE %I.schema_migration FROM PUBLIC, manzil_app',
                   target_schema);
END
$migration$;
