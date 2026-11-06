// Keys for the nodes the seed loader adds (BE-1.4).
CREATE CONSTRAINT snapshot_id_unique IF NOT EXISTS
FOR (n:RunningSnapshot) REQUIRE n.snapshot_id IS UNIQUE;
CREATE CONSTRAINT access_record_id_unique IF NOT EXISTS
FOR (n:AccessRecord) REQUIRE n.record_id IS UNIQUE;
CREATE INDEX snapshot_valid_from IF NOT EXISTS FOR (n:RunningSnapshot) ON (n.valid_from);
CREATE INDEX assertion_origin IF NOT EXISTS FOR (n:ContextAssertion) ON (n.origin);
