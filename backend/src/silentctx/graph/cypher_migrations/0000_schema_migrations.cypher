// Bookkeeping for the migration runner (graph/migrations.py).
// The domain schema (constraints, range and vector indexes) is BE-1.1.
CREATE CONSTRAINT schema_migration_id IF NOT EXISTS
FOR (m:SchemaMigration) REQUIRE m.id IS UNIQUE;
