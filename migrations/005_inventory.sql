-- Task 3.3: inventory schema. Design and assumptions: docs/INVENTORY_SCHEMA.md.
-- Run as the schema owner, never as the runtime login. Requires migration 002
-- (manzil_app, current_agency_id()).

-- pg_trgm is a trusted extension: creating it needs CREATE on the database.
-- Reuse an existing installation wherever it lives; never grant privileges here.
DO $extension$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_extension WHERE extname = 'pg_trgm') THEN
        IF NOT pg_catalog.has_database_privilege(current_database(), 'CREATE') THEN
            RAISE EXCEPTION 'pg_trgm is not installed and the migration role cannot create it';
        END IF;
        EXECUTE format('CREATE EXTENSION pg_trgm WITH SCHEMA %I', current_schema());
    END IF;
END
$extension$;


-- Shared gazetteer: one canonical geography for every agency.
CREATE TABLE location (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id UUID,
    level TEXT NOT NULL,
    parent_level TEXT,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    in_coverage BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (id, level),

    -- Strict city -> area -> sub_area; parent_id and parent_level are set together.
    CHECK (
        (level = 'city' AND parent_id IS NULL AND parent_level IS NULL)
        OR (level = 'area' AND parent_id IS NOT NULL AND parent_level = 'city')
        OR (level = 'sub_area' AND parent_id IS NOT NULL AND parent_level = 'area')
    ),

    FOREIGN KEY (parent_id, parent_level)
        REFERENCES location(id, level)
);


CREATE TABLE location_alias (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id UUID NOT NULL REFERENCES location(id),
    alias TEXT NOT NULL CHECK (btrim(alias) <> ''),
    alias_norm TEXT NOT NULL GENERATED ALWAYS AS (
        lower(regexp_replace(btrim(alias), '\s+', ' ', 'g'))
    ) STORED,
    -- The alias only applies inside this area; NULL means it applies everywhere.
    context_parent_id UUID REFERENCES location(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One alias may name several locations (ambiguity); exact duplicates are rejected.
    UNIQUE NULLS NOT DISTINCT (location_id, alias_norm, context_parent_id),

    CHECK (context_parent_id IS DISTINCT FROM location_id)
);


-- Symmetric adjacency, stored once per pair.
CREATE TABLE location_neighbor (
    location_a_id UUID NOT NULL REFERENCES location(id),
    location_b_id UUID NOT NULL REFERENCES location(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (location_a_id, location_b_id),

    CHECK (location_a_id < location_b_id)
);


-- Agency-owned inventory.
CREATE TABLE project (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    location_id UUID REFERENCES location(id),
    developer_name TEXT CHECK (btrim(developer_name) <> ''),
    construction_status TEXT
        CHECK (construction_status IN ('ready', 'under_construction')),
    is_demo BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id)
);


CREATE TABLE property (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    listing_code TEXT NOT NULL CHECK (listing_code ~ '^[A-Z0-9]+-[0-9]+$'),
    status TEXT NOT NULL
        CHECK (status IN ('active', 'reserved', 'sold', 'rented', 'withdrawn')),
    -- Unknown facts stay NULL; publication requirements are checked below.
    purpose TEXT CHECK (purpose IN ('sale', 'rent')),
    property_type TEXT
        CHECK (
            property_type IN (
                'house', 'apartment', 'portion', 'office', 'shop', 'plot'
            )
        ),
    category TEXT CHECK (category IN ('residential', 'commercial')),
    location_id UUID REFERENCES location(id),
    project_id UUID,
    -- Whole rupees: total for sale, per month for rent.
    price_pkr BIGINT CHECK (price_pkr > 0),
    area_value NUMERIC(12,2) CHECK (area_value > 0),
    area_unit TEXT CHECK (area_unit IN ('sq_ft', 'sq_yd', 'marla', 'kanal')),
    -- Pakistan conventions: 1 sq yd = 9 sq ft, 1 marla = 225 sq ft, 1 kanal = 20 marla.
    area_sqft NUMERIC GENERATED ALWAYS AS (
        area_value * CASE area_unit
            WHEN 'sq_ft' THEN 1
            WHEN 'sq_yd' THEN 9
            WHEN 'marla' THEN 225
            WHEN 'kanal' THEN 4500
        END
    ) STORED,
    bedrooms SMALLINT CHECK (bedrooms >= 0),
    bathrooms SMALLINT CHECK (bathrooms >= 0),
    construction_status TEXT
        CHECK (construction_status IN ('ready', 'under_construction')),
    latitude NUMERIC(9,6) CHECK (latitude BETWEEN -90 AND 90),
    longitude NUMERIC(9,6) CHECK (longitude BETWEEN -180 AND 180),
    -- Customer-safe known facts only; internal remarks belong in internal_notes.
    public_features JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(public_features) = 'object'),
    source_text TEXT,
    internal_notes TEXT,
    is_demo BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    cover_media_id UUID,
    created_by_staff_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, listing_code),

    FOREIGN KEY (agency_id, project_id)
        REFERENCES project(agency_id, id),

    FOREIGN KEY (agency_id, created_by_staff_id)
        REFERENCES staff(agency_id, id),

    CHECK ((area_value IS NULL) = (area_unit IS NULL)),
    CHECK ((latitude IS NULL) = (longitude IS NULL)),

    -- Minimum-to-save set (Demo Scope S4) for customer-visible listings.
    CONSTRAINT property_active_publishable CHECK (
        status <> 'active'
        OR (
            purpose IS NOT NULL
            AND property_type IS NOT NULL
            AND location_id IS NOT NULL
            AND price_pkr IS NOT NULL
            AND area_value IS NOT NULL
            AND (
                property_type NOT IN ('house', 'apartment', 'portion')
                OR bedrooms IS NOT NULL
            )
        )
    )
);


CREATE TABLE property_media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    property_id UUID NOT NULL,
    original_key TEXT NOT NULL CHECK (btrim(original_key) <> ''),
    wa_image_key TEXT CHECK (btrim(wa_image_key) <> ''),
    card_image_key TEXT CHECK (btrim(card_image_key) <> ''),
    content_sha256 TEXT CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    mime_type TEXT CHECK (btrim(mime_type) <> ''),
    sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, property_id, id),
    UNIQUE (agency_id, property_id, content_sha256),

    -- Reorder inside one transaction with SET CONSTRAINTS ... DEFERRED.
    CONSTRAINT property_media_order_key
        UNIQUE (agency_id, property_id, sort_order)
        DEFERRABLE INITIALLY IMMEDIATE,

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id)
);


-- The cover is media of the same agency and the same property. Deleting it
-- clears only cover_media_id; moving it to another property is rejected.
ALTER TABLE property
    ADD CONSTRAINT property_cover_media_fkey
        FOREIGN KEY (agency_id, id, cover_media_id)
        REFERENCES property_media(agency_id, property_id, id)
        ON DELETE SET NULL (cover_media_id);


CREATE TABLE listing_draft (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    staff_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'update')),
    target_property_id UUID,
    -- The property version the draft was based on; saving checks it atomically.
    base_property_version INTEGER CHECK (base_property_version > 0),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'saved', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,

    UNIQUE (agency_id, id),

    FOREIGN KEY (agency_id, staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, target_property_id)
        REFERENCES property(agency_id, id),

    CHECK (
        (
            kind = 'create'
            AND target_property_id IS NULL
            AND base_property_version IS NULL
        )
        OR (
            kind = 'update'
            AND target_property_id IS NOT NULL
            AND base_property_version IS NOT NULL
        )
    ),

    CHECK ((status = 'open') = (closed_at IS NULL))
);


CREATE TABLE property_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    property_id UUID NOT NULL,
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'created', 'updated', 'status_changed',
                'media_added', 'media_removed', 'undone'
            )
        ),
    actor_staff_id UUID,
    listing_draft_id UUID,
    from_version INTEGER CHECK (from_version > 0),
    to_version INTEGER NOT NULL CHECK (to_version > 0),
    changes JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(changes) = 'object'),
    undoes_event_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, property_id, id),

    FOREIGN KEY (agency_id, property_id)
        REFERENCES property(agency_id, id),

    FOREIGN KEY (agency_id, actor_staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, listing_draft_id)
        REFERENCES listing_draft(agency_id, id),

    -- An undo can only reverse an event of the same property.
    FOREIGN KEY (agency_id, property_id, undoes_event_id)
        REFERENCES property_event(agency_id, property_id, id),

    CHECK (from_version IS NULL OR from_version < to_version),
    CHECK ((event_type = 'undone') = (undoes_event_id IS NOT NULL))
);


-- Temporary staff WhatsApp workflow context; not authentication.
CREATE TABLE staff_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agency_id UUID NOT NULL REFERENCES agency(id),
    staff_id UUID NOT NULL,
    focus_property_id UUID,
    state JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(state) = 'object'),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (agency_id, id),
    UNIQUE (agency_id, staff_id),

    FOREIGN KEY (agency_id, staff_id)
        REFERENCES staff(agency_id, id),

    FOREIGN KEY (agency_id, focus_property_id)
        REFERENCES property(agency_id, id)
);


-- Customer-safe listing allowlist. Not SELECT *: new columns stay private.
CREATE VIEW v_public_listing
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    id,
    agency_id,
    listing_code,
    status,
    purpose,
    property_type,
    category,
    location_id,
    project_id,
    price_pkr,
    area_value,
    area_unit,
    area_sqft,
    bedrooms,
    bathrooms,
    construction_status,
    latitude,
    longitude,
    public_features,
    cover_media_id,
    version,
    updated_at
FROM property
WHERE status = 'active';


-- Gazetteer lookups.
CREATE INDEX location_parent_idx
    ON location (parent_id);

CREATE UNIQUE INDEX location_parent_name_key
    ON location (parent_id, lower(name)) NULLS NOT DISTINCT;

CREATE INDEX location_alias_exact_idx
    ON location_alias (alias_norm, context_parent_id);

CREATE INDEX location_alias_location_idx
    ON location_alias (location_id);

CREATE INDEX location_alias_context_idx
    ON location_alias (context_parent_id);

CREATE INDEX location_neighbor_b_idx
    ON location_neighbor (location_b_id);


-- Inventory lookups and active-listing search.
CREATE UNIQUE INDEX project_agency_name_key
    ON project (agency_id, lower(name));

CREATE INDEX project_location_idx
    ON project (agency_id, location_id);

CREATE INDEX property_active_search_idx
    ON property (agency_id, purpose, property_type, price_pkr)
    WHERE status = 'active';

CREATE INDEX property_active_location_idx
    ON property (agency_id, location_id)
    WHERE status = 'active';

CREATE INDEX property_project_idx
    ON property (agency_id, project_id);

CREATE INDEX property_created_by_idx
    ON property (agency_id, created_by_staff_id);

CREATE INDEX property_event_history_idx
    ON property_event (agency_id, property_id, created_at);

CREATE INDEX property_event_draft_idx
    ON property_event (agency_id, listing_draft_id);

-- One open update draft per staff member and target property; create drafts are unlimited.
CREATE UNIQUE INDEX listing_draft_one_open_update_idx
    ON listing_draft (agency_id, staff_id, target_property_id)
    WHERE status = 'open' AND kind = 'update';

CREATE INDEX listing_draft_open_staff_idx
    ON listing_draft (agency_id, staff_id)
    WHERE status = 'open';

CREATE INDEX listing_draft_target_idx
    ON listing_draft (agency_id, target_property_id);

CREATE INDEX staff_session_focus_idx
    ON staff_session (agency_id, focus_property_id);


-- Trigram index; the operator class lives wherever pg_trgm is installed.
-- Privileges and RLS follow the 002 conventions.
DO $migration$
DECLARE
    target_schema TEXT := current_schema();
    trgm_schema TEXT;
    app_role RECORD;
    table_name TEXT;
BEGIN
    SELECT n.nspname INTO STRICT trgm_schema
    FROM pg_catalog.pg_extension e
    JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'pg_trgm';

    EXECUTE format(
        'CREATE INDEX location_alias_trgm_idx ON %I.location_alias '
        'USING gin (alias_norm %I.gin_trgm_ops)',
        target_schema, trgm_schema
    );

    SELECT * INTO app_role
    FROM pg_catalog.pg_roles WHERE rolname = 'manzil_app';

    IF NOT FOUND OR app_role.rolsuper OR app_role.rolbypassrls THEN
        RAISE EXCEPTION 'manzil_app is missing or has unsafe attributes';
    END IF;

    FOREACH table_name IN ARRAY ARRAY[
        'location', 'location_alias', 'location_neighbor',
        'project', 'property', 'property_media', 'property_event',
        'listing_draft', 'staff_session', 'v_public_listing'
    ] LOOP
        EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM PUBLIC, manzil_app',
                       target_schema, table_name);
    END LOOP;

    -- Shared gazetteer: read-only for the runtime.
    FOREACH table_name IN ARRAY ARRAY[
        'location', 'location_alias', 'location_neighbor', 'v_public_listing'
    ] LOOP
        EXECUTE format('GRANT SELECT ON TABLE %I.%I TO manzil_app',
                       target_schema, table_name);
    END LOOP;

    -- Listings and projects leave by status, never by DELETE.
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE %I.project, %I.property TO manzil_app',
                   target_schema, target_schema);
    -- Audit log: append-only.
    EXECUTE format('GRANT SELECT, INSERT ON TABLE %I.property_event TO manzil_app',
                   target_schema);
    EXECUTE format(
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
        '%I.property_media, %I.listing_draft, %I.staff_session TO manzil_app',
        target_schema, target_schema, target_schema
    );

    FOREACH table_name IN ARRAY ARRAY[
        'project', 'property', 'property_media', 'property_event',
        'listing_draft', 'staff_session'
    ] LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY',
                       target_schema, table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I.%I FOR ALL TO manzil_app '
            'USING (agency_id = %I.current_agency_id()) '
            'WITH CHECK (agency_id = %I.current_agency_id())',
            target_schema, table_name, target_schema, target_schema
        );
    END LOOP;
END
$migration$;
