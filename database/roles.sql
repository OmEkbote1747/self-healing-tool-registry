-- OPTIONAL but recommended: a least-privilege role for ADAPTIX on Neon.
-- Run this ONCE in Neon's SQL Editor as the project owner, BEFORE init_demo.sql.
-- Replace <CHOOSE_A_STRONG_PASSWORD> in the editor only. Never commit the real password.
--
-- The role owns only the two demo schemas, so it can create, alter and reset the demo tables there and nothing else.

CREATE ROLE adaptix_app LOGIN PASSWORD '<CHOOSE_A_STRONG_PASSWORD>';

CREATE SCHEMA IF NOT EXISTS adaptix_demo AUTHORIZATION adaptix_app;
CREATE SCHEMA IF NOT EXISTS adaptix_demo_sandbox AUTHORIZATION adaptix_app;

-- No access to anything else (PostgreSQL 15+ already denies CREATE on public to ordinary roles).
REVOKE ALL ON SCHEMA public FROM adaptix_app;

-- Next: run init_demo.sql as adaptix_app, or let the app do it (Live Project Integration -> Set up / reset).
-- If you run init_demo.sql as the OWNER instead, transfer ownership so ADAPTIX can run the demo faults:
--   DO $$ DECLARE r record; BEGIN
--     FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'adaptix_demo' LOOP
--       EXECUTE format('ALTER TABLE adaptix_demo.%I OWNER TO adaptix_app', r.tablename);
--     END LOOP; END $$;
