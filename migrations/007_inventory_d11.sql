-- Task 3.3 D11 alignment: the gaps of applied migration 005 against the D11 data
-- model (R11-R13) and R17, listed in docs/SPEC_ALIGNMENT.md section 2. Additive:
-- every new column is nullable or has a constant default, and no row is changed
-- by an UPDATE (FORCE RLS would hide rows from the owner). The only table
-- rewrite is property's area_sqft expression. Locks and the RDS runbook:
-- docs/OPEN_ISSUES.md. Run as the schema owner, never as the runtime login.
-- Requires 002, 005, 006.
-- Media columns (G16) belong to Task 4.1; draft fields (G17) to Tasks 7.4 / 9.2.


-- A text array is a set drawn from an allowed vocabulary (no NULLs, no repeats).
-- STRICT: a NULL array is an unknown fact and passes.
CREATE FUNCTION text_set_within(items TEXT[], allowed TEXT[]) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT items <@ allowed
        AND cardinality(items) = (SELECT count(DISTINCT item) FROM unnest(items) AS item)
$$;


-- 005 CHECKs replaced below. Each must still be the unnamed 005 constraint; a
-- schema that has drifted fails here instead of silently losing a rule.
DO $replace$
DECLARE
    item RECORD;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('location', 'location_check'),
            ('project', 'project_construction_status_check'),
            ('property', 'property_status_check'),
            ('property', 'property_property_type_check'),
            ('property', 'property_construction_status_check'),
            ('property', 'property_active_publishable'),
            ('property_event', 'property_event_event_type_check')
        ) AS v(table_name, constraint_name)
    LOOP
        IF NOT EXISTS (
            SELECT FROM pg_catalog.pg_constraint
            WHERE conrelid = format('%I.%I', current_schema(), item.table_name)::regclass
              AND conname = item.constraint_name
              AND contype = 'c'
        ) THEN
            RAISE EXCEPTION 'expected constraint %.% is missing', item.table_name,
                item.constraint_name;
        END IF;
        EXECUTE format('ALTER TABLE %I.%I DROP CONSTRAINT %I', current_schema(),
                       item.table_name, item.constraint_name);
    END LOOP;
END
$replace$;


-- Gazetteer (G1, G2, G10-G12). name stays the canonical name.
ALTER TABLE location
    ADD COLUMN display_name TEXT CHECK (btrim(display_name) <> ''),
    -- Listing-code prefix ({AREA_CODE}-{seq}): DH6, D2X, DHC, CLF, BTK ...
    ADD COLUMN code TEXT UNIQUE CHECK (code ~ '^[A-Z0-9]{2,4}$'),
    -- Lets a bare "phase 8" / "block 6" be read against the active area (R17 3.2).
    ADD COLUMN level_label TEXT
        CHECK (level_label IN ('phase', 'precinct', 'block', 'sector', 'zone', 'corridor')),
    ADD COLUMN city_code TEXT CHECK (city_code ~ '^[A-Z]{3}$'),
    -- Area centroid for location messages (demo: no exact addresses).
    ADD COLUMN centroid_lat NUMERIC(9,6) CHECK (centroid_lat BETWEEN -90 AND 90),
    ADD COLUMN centroid_lng NUMERIC(9,6) CHECK (centroid_lng BETWEEN -180 AND 180),
    ADD COLUMN is_commercial_zone BOOLEAN NOT NULL DEFAULT FALSE,

    -- city -> area -> sub_area -> zone. A zone sits under a sub-area (Ph 6 >
    -- Bukhari Commercial) or directly under an area (PECHS > Tariq Road).
    ADD CONSTRAINT location_hierarchy_check CHECK (
        (level = 'city' AND parent_id IS NULL AND parent_level IS NULL)
        OR (level = 'area' AND parent_id IS NOT NULL AND parent_level = 'city')
        OR (level = 'sub_area' AND parent_id IS NOT NULL AND parent_level = 'area')
        OR (level = 'zone' AND parent_id IS NOT NULL AND parent_level IN ('area', 'sub_area'))
    ),
    ADD CONSTRAINT location_city_code_level_check CHECK (city_code IS NULL OR level = 'city'),
    ADD CONSTRAINT location_centroid_check
        CHECK ((centroid_lat IS NULL) = (centroid_lng IS NULL));

ALTER TABLE location_alias
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'common'
        CHECK (kind IN ('official', 'common', 'misspelling', 'urdu_script', 'voice_stt')),
    ADD COLUMN weight NUMERIC(3,2) NOT NULL DEFAULT 1 CHECK (weight BETWEEN 0 AND 1);

-- Relaxation-ladder weight of an adjacency.
ALTER TABLE location_neighbor
    ADD COLUMN weight NUMERIC(3,2) NOT NULL DEFAULT 1 CHECK (weight > 0 AND weight <= 1);


-- Projects (G3, G15).
ALTER TABLE project
    ADD COLUMN project_type TEXT
        CHECK (project_type IN (
            'apartment_tower', 'mixed_use', 'commercial_mall', 'housing_scheme'
        )),
    -- Month precision: stored as the first day of the month.
    ADD COLUMN expected_completion DATE
        CHECK (expected_completion = date_trunc('month', expected_completion)::date),
    ADD COLUMN payment_plan_summary TEXT CHECK (btrim(payment_plan_summary) <> ''),
    ADD COLUMN amenities TEXT[] CHECK (array_position(amenities, NULL) IS NULL),
    -- Internal (approval / NOC status, verified by staff); never in a view.
    ADD COLUMN approval_note TEXT,

    ADD CONSTRAINT project_construction_status_check
        CHECK (construction_status IN (
            'ready', 'under_construction', 'grey_structure', 'off_plan'
        ));


-- Agency settings (G18). marla_sqft is the agency's marla convention (D11 4.1:
-- DHA 225 by default, traditional 272.25). Listings reference (id, marla_sqft),
-- so the setting cannot change while listings were measured with it.
ALTER TABLE agency
    ADD COLUMN city_default TEXT NOT NULL DEFAULT 'KHI' CHECK (city_default ~ '^[A-Z]{3}$'),
    ADD COLUMN marla_sqft NUMERIC(6,2) NOT NULL DEFAULT 225
        CHECK (marla_sqft IN (225, 272.25)),
    ADD COLUMN settings JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(settings) = 'object'),

    ADD CONSTRAINT agency_marla_basis_key UNIQUE (id, marla_sqft);


-- Listings (G3-G8, G13, G14).
ALTER TABLE property
    -- Typed facts that search filters or ranks on (R17 2.1). Unknown = NULL.
    ADD COLUMN possession_status TEXT
        CHECK (possession_status IN ('possession', 'on_ground', 'file', 'balloted', 'allotted')),
    ADD COLUMN expected_possession DATE
        CHECK (expected_possession = date_trunc('month', expected_possession)::date),
    ADD COLUMN installment_available BOOLEAN,
    ADD COLUMN is_tenanted BOOLEAN,
    ADD COLUMN investment_tags TEXT[]
        CHECK (text_set_within(investment_tags, ARRAY[
            'rental_income', 'capital_gain', 'pre_launch', 'installments'
        ])),
    -- Market label bucket in sq yd (Bahria "250 yd" plot is 272 actual: area_value
    -- 272 sq_yd, size_nominal_sqyd 250). area_value / area_unit stay the raw size.
    ADD COLUMN size_nominal_sqyd INTEGER CHECK (size_nominal_sqyd > 0),
    ADD COLUMN covered_area_sqft NUMERIC(12,2) CHECK (covered_area_sqft > 0),
    -- Last confirmed by staff (staleness, ranking freshness). Rows that exist
    -- before 007 stay NULL = never confirmed; the default is set separately below.
    ADD COLUMN last_confirmed_at TIMESTAMPTZ,
    -- The agency's marla_sqft when a marla / kanal size was entered; NULL for
    -- other units. area_sqft converts with it (see SET EXPRESSION below).
    ADD COLUMN area_marla_sqft NUMERIC(6,2),
    ADD COLUMN furnishing TEXT CHECK (furnishing IN ('unfurnished', 'semi', 'furnished')),
    ADD COLUMN facing TEXT[]
        CHECK (text_set_within(facing, ARRAY[
            'west_open', 'east_open', 'north_open', 'south_open',
            'park_facing', 'road_facing', 'sea_facing'
        ])),
    ADD COLUMN is_corner BOOLEAN,
    ADD COLUMN features TEXT[]
        CHECK (text_set_within(features, ARRAY[
            'basement', 'annex', 'servant_quarter', 'lift', 'generator_backup', 'solar',
            'gas_connection', 'water_kwsb', 'water_boring', 'security', 'lawn',
            'roof_access', 'sea_view', 'main_boulevard', 'drawing_dining'
        ])),
    ADD COLUMN portion_level TEXT
        CHECK (portion_level IN ('ground', 'first', 'second', 'upper', 'lower')),
    ADD COLUMN year_built SMALLINT CHECK (year_built BETWEEN 1900 AND 2100),

    -- Internal only (D11 1.1): never in v_public_listing or a customer prompt.
    ADD COLUMN source_type TEXT
        CHECK (source_type IN ('owner_direct', 'agent_listing', 'sub_agent', 'developer')),
    ADD COLUMN owner_name TEXT CHECK (btrim(owner_name) <> ''),
    ADD COLUMN owner_phone TEXT CHECK (btrim(owner_phone) <> ''),
    ADD COLUMN floor_price_pkr BIGINT CHECK (floor_price_pkr > 0),
    ADD COLUMN commission_pct NUMERIC(5,2) CHECK (commission_pct BETWEEN 0 AND 100),
    ADD COLUMN commission_note TEXT,
    ADD COLUMN is_exclusive BOOLEAN,
    ADD COLUMN assigned_broker_id UUID,
    ADD COLUMN docs_status TEXT
        CHECK (docs_status IN (
            'leased', 'transfer_ready', 'noc_pending', 'file_only', 'unknown'
        )),
    ADD COLUMN visit_notes TEXT,
    -- Street / plot number: shared by the broker after handoff, never by the AI.
    ADD COLUMN address_detail TEXT,
    -- WhatsApp WAMIDs that created or changed the listing (R3).
    ADD COLUMN source_message_ids TEXT[]
        CHECK (array_position(source_message_ids, NULL) IS NULL),
    -- Possible duplicate listings share a group (R17 edge 14).
    ADD COLUMN duplicate_group_id UUID,

    ADD CONSTRAINT property_assigned_broker_fkey
        FOREIGN KEY (agency_id, assigned_broker_id)
        REFERENCES staff(agency_id, id),

    -- The basis must be the listing agency's current marla convention. A row
    -- from before 007 with a marla / kanal size has none and fails validation,
    -- rolling the migration back (fail closed; validation is not subject to RLS).
    ADD CONSTRAINT property_area_marla_basis_fkey
        FOREIGN KEY (agency_id, area_marla_sqft)
        REFERENCES agency(id, marla_sqft),
    ADD CONSTRAINT property_area_marla_basis_check CHECK (
        CASE WHEN area_unit IN ('marla', 'kanal') THEN area_marla_sqft IS NOT NULL
             ELSE area_marla_sqft IS NULL
        END
    ),

    ADD CONSTRAINT property_status_check
        CHECK (status IN (
            'active', 'reserved', 'on_hold', 'sold', 'rented', 'withdrawn', 'archived'
        )),
    ADD CONSTRAINT property_property_type_check
        CHECK (property_type IN (
            'house', 'portion', 'apartment', 'penthouse', 'plot',
            'shop', 'office', 'showroom', 'warehouse', 'building', 'floor'
        )),
    ADD CONSTRAINT property_construction_status_check
        CHECK (construction_status IN (
            'ready', 'under_construction', 'grey_structure', 'off_plan'
        )),
    ADD CONSTRAINT property_portion_level_type_check
        CHECK (portion_level IS NULL OR property_type = 'portion'),

    -- Display-only facts live in public_features; only these keys are allowed,
    -- so internal data cannot be parked there. Search never filters on them.
    ADD CONSTRAINT property_public_features_keys_check CHECK (
        public_features - ARRAY[
            'title', 'description', 'floor_no', 'total_floors', 'parking_spaces',
            'price_negotiable', 'rent_security_deposit_pkr', 'rent_advance_months',
            'maintenance_pkr_month', 'min_lease_months', 'available_from',
            'down_payment_pkr', 'installment_months', 'installment_amount_pkr',
            'current_rent_pkr'
        ] = '{}'::jsonb
    ),
    ADD CONSTRAINT property_public_features_text_check CHECK (
        (
            NOT public_features ? 'title'
            OR (
                jsonb_typeof(public_features -> 'title') = 'string'
                AND char_length(btrim(public_features ->> 'title')) BETWEEN 1 AND 60
            )
        )
        AND (
            NOT public_features ? 'description'
            OR (
                jsonb_typeof(public_features -> 'description') = 'string'
                AND char_length(btrim(public_features ->> 'description')) BETWEEN 1 AND 600
            )
        )
    ),

    -- Minimum-to-save set (Demo Scope S4), now with penthouses as residential.
    ADD CONSTRAINT property_active_publishable CHECK (
        status <> 'active'
        OR (
            purpose IS NOT NULL
            AND property_type IS NOT NULL
            AND location_id IS NOT NULL
            AND price_pkr IS NOT NULL
            AND area_value IS NOT NULL
            AND (
                property_type NOT IN ('house', 'apartment', 'penthouse', 'portion')
                OR bedrooms IS NOT NULL
            )
        )
    );

-- New listings are confirmed when staff save them; existing rows keep NULL.
ALTER TABLE property ALTER COLUMN last_confirmed_at SET DEFAULT now();

-- Marla and kanal convert with the listing's agency basis, not a fixed 225
-- (kanal = 20 marla, D11 4.1). Rewrites property (empty or tiny before 4.3).
ALTER TABLE property ALTER COLUMN area_sqft SET EXPRESSION AS (
    area_value * CASE area_unit
        WHEN 'sq_ft' THEN 1
        WHEN 'sq_yd' THEN 9
        WHEN 'marla' THEN area_marla_sqft
        WHEN 'kanal' THEN 20 * area_marla_sqft
    END
);


-- Audit (G9): reconfirmation events and the message that caused an event. A
-- reconfirmed event records the current version (from_version NULL).
ALTER TABLE property_event
    ADD COLUMN source_message_id UUID,

    ADD CONSTRAINT property_event_source_message_fkey
        FOREIGN KEY (agency_id, source_message_id)
        REFERENCES message(agency_id, id)
        ON DELETE SET NULL (source_message_id),

    ADD CONSTRAINT property_event_event_type_check
        CHECK (event_type IN (
            'created', 'updated', 'status_changed', 'reconfirmed',
            'media_added', 'media_removed', 'undone'
        ));


-- Location IDs a broker covers (lead routing, 11.1); not referential.
ALTER TABLE staff
    ADD COLUMN areas_covered UUID[] NOT NULL DEFAULT '{}'
        CHECK (array_position(areas_covered, NULL) IS NULL);


-- Customer-safe allowlist (G19). New customer-visible columns are appended so
-- the existing columns and grants are kept; internal columns stay out.
CREATE OR REPLACE VIEW v_public_listing
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
    updated_at,
    size_nominal_sqyd,
    covered_area_sqft,
    portion_level,
    furnishing,
    facing,
    is_corner,
    features,
    year_built,
    possession_status,
    expected_possession,
    installment_available,
    is_tenanted,
    investment_tags,
    last_confirmed_at
FROM property
WHERE status = 'active';

-- Projects as customers may see them (name, plan, completion); no approval_note.
CREATE VIEW v_public_project
WITH (security_invoker = true, security_barrier = true) AS
SELECT
    id,
    agency_id,
    name,
    location_id,
    developer_name,
    project_type,
    construction_status,
    expected_completion,
    payment_plan_summary,
    amenities,
    updated_at
FROM project;


CREATE INDEX location_level_label_idx
    ON location (parent_id, level_label);

CREATE INDEX property_assigned_broker_idx
    ON property (agency_id, assigned_broker_id);

CREATE INDEX property_event_source_message_idx
    ON property_event (agency_id, source_message_id);


-- Privileges follow the 002 / 005 conventions.
DO $migration$
DECLARE
    target_schema TEXT := current_schema();
BEGIN
    EXECUTE format('REVOKE ALL ON TABLE %I.v_public_project FROM PUBLIC, manzil_app',
                   target_schema);
    EXECUTE format('GRANT SELECT ON TABLE %I.v_public_project TO manzil_app', target_schema);
    -- CHECK constraints call this as the writing role.
    EXECUTE format('REVOKE ALL ON FUNCTION %I.text_set_within(TEXT[], TEXT[]) FROM PUBLIC',
                   target_schema);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %I.text_set_within(TEXT[], TEXT[]) TO manzil_app',
                   target_schema);
END
$migration$;
