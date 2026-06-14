-- ============================================================================
-- Query Bench demo database — single-file setup (PostGIS + pgvector).
--
-- Convenience artifact: this is init/01_schema.sql + init/02_seed.sql combined
-- so you can load the whole demo in ONE paste/run on any PostgreSQL that has
-- the PostGIS and pgvector extensions available — e.g. a hosted Supabase /
-- Azure Database for PostgreSQL Flexible Server, or a local PostgreSQL with
-- PostGIS (StackBuilder) and pgvector installed.
--
-- How to run:
--   * psql:        psql "<connection-string>" -f demo/setup_hosted.sql
--   * Supabase:    paste into the SQL Editor and Run
--   * Azure/pgAdmin/DBeaver: open this file and execute
--
-- After loading, populate product embeddings for pgvector semantic search:
--   python demo/seed_embeddings.py     (set DEMO_DB_* env vars first)
--
-- Generates: 100 stores, 5,000 customers, 30,000 orders, 24 products — all
-- geospatial (SRID 4326). Reproducible (setseed) and guarded against re-runs.
-- ============================================================================

-- 1) Extensions the agent detects via detect_extensions().
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- 2) Stores — each has a PostGIS point geometry (SRID 4326, lon/lat).
CREATE TABLE IF NOT EXISTS stores (
    store_id   SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    city       TEXT,
    region     TEXT,
    geom       geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stores_geom ON stores USING GIST (geom);

-- 3) Customers — each has a PostGIS home location (SRID 4326, lon/lat).
CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT UNIQUE,
    signup_date DATE NOT NULL,
    city        TEXT,
    geom        geometry(Point, 4326)
);
CREATE INDEX IF NOT EXISTS idx_customers_geom ON customers USING GIST (geom);

-- 4) Orders — net revenue = amount - discount - refund (tax excluded).
CREATE TABLE IF NOT EXISTS orders (
    order_id    SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  DATE NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('completed','pending','cancelled','returned')),
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
    discount    NUMERIC(10,2) NOT NULL DEFAULT 0,
    refund      NUMERIC(10,2) NOT NULL DEFAULT 0,
    tax         NUMERIC(10,2) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders (status, order_date);

-- 5) Products — description embedding for pgvector semantic search.
CREATE TABLE IF NOT EXISTS products (
    product_id  SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    price       NUMERIC(10,2) NOT NULL DEFAULT 0,
    embedding   vector(384)
);

-- ── Seed (procedural, reproducible, guarded) ────────────────────────────────
DO $seed$
DECLARE
    first_names TEXT[] := ARRAY['Ava','Liam','Mia','Noah','Emma','Oliver','Sophia',
        'Lucas','Isabella','Ethan','Amelia','Mason','Harper','Logan','Ella','James',
        'Aria','Ben','Chloe','Henry','Layla','Jack','Nora','Leo','Zoe'];
    last_names TEXT[] := ARRAY['Thompson','Nguyen','Patel','Garcia','Wilson','Brown',
        'Davis','Martin','Lee','Clark','Lewis','Walker','Hall','Young','King','Wright',
        'Scott','Green','Adams','Baker','Rivera','Hughes','Price','Bennett','Reed'];
    cities TEXT[] := ARRAY['Seattle','Bellevue','Redmond','Kirkland','Renton',
        'Tacoma','Everett','Bothell'];
BEGIN
    IF (SELECT count(*) FROM customers) > 0 THEN
        RAISE NOTICE 'Demo data already present — skipping seed.';
        RETURN;
    END IF;

    PERFORM setseed(0.42);

    -- Stores: 15 clustered within ~3-4 km of downtown (-122.3321, 47.6062).
    INSERT INTO stores (name, city, region, geom)
    SELECT
        'Store #' || g, 'Seattle', 'WA',
        ST_SetSRID(ST_MakePoint(
            -122.3321 + (random() - 0.5) * 0.06,
            47.6062  + (random() - 0.5) * 0.06
        ), 4326)
    FROM generate_series(1, 15) AS g;

    -- Stores: 85 scattered across the wider metro bounding box.
    INSERT INTO stores (name, city, region, geom)
    SELECT
        'Store #' || (g + 15),
        cities[1 + floor(random() * array_length(cities, 1))::int], 'WA',
        ST_SetSRID(ST_MakePoint(
            -122.45 + random() * 0.35,
            47.45  + random() * 0.30
        ), 4326)
    FROM generate_series(1, 85) AS g;

    -- Customers: 5,000 with home location + signup over ~3 years.
    INSERT INTO customers (name, email, signup_date, city, geom)
    SELECT
        first_names[1 + floor(random() * array_length(first_names, 1))::int]
            || ' ' ||
        last_names[1 + floor(random() * array_length(last_names, 1))::int],
        'customer' || g || '@example.com',
        (CURRENT_DATE - (floor(random() * 1095))::int)::date,
        cities[1 + floor(random() * array_length(cities, 1))::int],
        ST_SetSRID(ST_MakePoint(
            -122.45 + random() * 0.35,
            47.45  + random() * 0.30
        ), 4326)
    FROM generate_series(1, 5000) AS g;

    -- Orders: 30,000 (70% completed / 10% pending / 10% cancelled / 10% returned).
    INSERT INTO orders (customer_id, order_date, status, amount, discount, refund, tax)
    SELECT
        1 + floor(random() * 5000)::int,
        (CURRENT_DATE - (floor(random() * 400))::int)::date,
        (ARRAY['completed','completed','completed','completed','completed',
               'completed','completed','pending','cancelled','returned']
        )[1 + floor(random() * 10)::int],
        amt,
        round((amt * random() * 0.15)::numeric, 2),
        CASE WHEN random() < 0.08
             THEN round((amt * (0.3 + random() * 0.7))::numeric, 2)
             ELSE 0 END,
        round((amt * 0.09)::numeric, 2)
    FROM (
        SELECT round((20 + random() * 1980)::numeric, 2) AS amt
        FROM generate_series(1, 30000)
    ) s;

    RAISE NOTICE 'Seeded 100 stores, 5000 customers, 30000 orders.';
END
$seed$;

-- Products (embeddings populated separately by demo/seed_embeddings.py).
INSERT INTO products (name, description, price)
SELECT * FROM (VALUES
    ('Trail Blazer Hiking Boots', 'Waterproof leather hiking boots with aggressive tread for rugged mountain trails.', 159.00),
    ('Summit Down Jacket',        'Lightweight 800-fill down jacket that keeps you warm in freezing winter conditions.', 220.00),
    ('Riverside Tent 2P',         'Two-person waterproof backpacking tent with a quick-pitch aluminium pole set.', 189.00),
    ('Trailhead Daypack 30L',     'Durable 30-litre daypack with hydration sleeve for long day hikes.', 95.00),
    ('Cascade Rain Shell',        'Breathable waterproof rain jacket with sealed seams for wet weather.', 130.00),
    ('Glacier Water Bottle',      'Insulated stainless-steel bottle that keeps drinks cold for 24 hours.', 32.00),
    ('Campfire Cook Set',         'Compact nesting camping cookware set for backcountry meals.', 64.00),
    ('Nordic Wool Socks',         'Warm merino wool hiking socks for cold-weather comfort.', 24.00),
    ('Carbon Trekking Poles',     'Ultralight adjustable carbon-fibre trekking poles for steep terrain.', 110.00),
    ('Sunrise Sleeping Bag',      'Three-season mummy sleeping bag rated to -5C for cold nights.', 145.00),
    ('Rapid Dry Towel',           'Quick-drying microfibre travel towel that packs down small.', 22.00),
    ('Alpine Headlamp',           'Rechargeable 400-lumen headlamp for night hiking and camp chores.', 41.00),
    ('Boulder Climbing Shoes',    'Sticky-rubber rock climbing shoes for bouldering and sport routes.', 135.00),
    ('Meadow Picnic Blanket',     'Water-resistant fleece picnic blanket for parks and beaches.', 38.00),
    ('Harbor Kayak Paddle',       'Lightweight aluminium kayak paddle with drip rings.', 78.00),
    ('Tundra Insulated Gloves',   'Windproof insulated gloves for skiing and snow sports.', 49.00),
    ('Canyon Trail Runners',      'Breathable trail running shoes with grippy lugged soles.', 118.00),
    ('Lakeside Camp Chair',       'Foldable lightweight camp chair with a cup holder.', 54.00),
    ('Forest Hammock',            'Packable double parachute-nylon hammock with tree straps.', 46.00),
    ('Polar Cooler 25L',          'Hard-sided 25-litre cooler that holds ice for three days.', 92.00),
    ('Drizzle Rain Pants',        'Packable waterproof over-trousers for hiking in the rain.', 69.00),
    ('Ember Camp Stove',          'Compact single-burner backpacking stove with piezo ignition.', 58.00),
    ('Ridge Sun Hat',             'Wide-brim UPF 50 sun hat for hot-weather hikes.', 29.00),
    ('Coastal Dry Bag 20L',       'Roll-top waterproof dry bag for kayaking and beach trips.', 34.00)
) AS v(name, description, price)
WHERE NOT EXISTS (SELECT 1 FROM products);

-- ── Quick verification (optional) ───────────────────────────────────────────
-- SELECT count(*) AS stores FROM stores;
-- SELECT count(*) AS customers FROM customers;
-- SELECT count(*) AS orders FROM orders;
-- Stores within 5 km of downtown:
-- SELECT name, ROUND(ST_Distance(geom::geography,
--          ST_SetSRID(ST_MakePoint(-122.3321,47.6062),4326)::geography)) AS metres
-- FROM stores
-- WHERE ST_DWithin(geom::geography,
--          ST_SetSRID(ST_MakePoint(-122.3321,47.6062),4326)::geography, 5000)
-- ORDER BY metres;
