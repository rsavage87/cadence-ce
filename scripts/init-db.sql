-- Runtime role for the application. It is NOT the table owner, so row-level security policies apply to it.
CREATE ROLE cadence_app LOGIN PASSWORD 'cadence_app';
GRANT CONNECT ON DATABASE cadence TO cadence_app;
GRANT USAGE ON SCHEMA public TO cadence_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cadence IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cadence_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cadence IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cadence_app;
