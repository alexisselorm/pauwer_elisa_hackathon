// Context-layer schema (plan 001 §5). Applied by neo4j-python-migrations, in one transaction.
// Every statement is idempotent. Never edit an applied file: add V0002__... instead
// (the tool checksums each file and refuses to run if an applied one changed).

// --- Uniqueness ---------------------------------------------------------------------------
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (n:Entity) REQUIRE n.entity_id IS UNIQUE;
CREATE CONSTRAINT assertion_id_unique IF NOT EXISTS
FOR (n:ContextAssertion) REQUIRE n.assertion_id IS UNIQUE;
CREATE CONSTRAINT event_id_unique IF NOT EXISTS
FOR (n:Event) REQUIRE n.event_id IS UNIQUE;
CREATE CONSTRAINT thread_id_unique IF NOT EXISTS
FOR (n:Thread) REQUIRE n.thread_id IS UNIQUE;
CREATE CONSTRAINT person_id_unique IF NOT EXISTS
FOR (n:Person) REQUIRE n.person_id IS UNIQUE;
CREATE CONSTRAINT gap_id_unique IF NOT EXISTS
FOR (n:KnowledgeGap) REQUIRE n.gap_id IS UNIQUE;
CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS
FOR (n:SourceChunk) REQUIRE n.chunk_id IS UNIQUE;

// --- Range indexes for as-of queries (R9/R10) ---------------------------------------------
CREATE INDEX assertion_valid_from IF NOT EXISTS FOR (n:ContextAssertion) ON (n.valid_from);
CREATE INDEX assertion_valid_to IF NOT EXISTS FOR (n:ContextAssertion) ON (n.valid_to);
CREATE INDEX assertion_recorded_at IF NOT EXISTS FOR (n:ContextAssertion) ON (n.recorded_at);
CREATE INDEX assertion_status IF NOT EXISTS FOR (n:ContextAssertion) ON (n.status);
CREATE INDEX assertion_topic IF NOT EXISTS FOR (n:ContextAssertion) ON (n.topic);
CREATE INDEX entity_valid_from IF NOT EXISTS FOR (n:Entity) ON (n.valid_from);
CREATE INDEX entity_valid_to IF NOT EXISTS FOR (n:Entity) ON (n.valid_to);
CREATE INDEX event_time IF NOT EXISTS FOR (n:Event) ON (n.event_time);

// --- Vector indexes (ranking only, retrieval is scoped by entity and time first) ----------
// 1024 = EMBEDDING_DIM (plan 001 §2). The API refuses readiness if the setting differs.
CREATE VECTOR INDEX assertion_embedding IF NOT EXISTS
FOR (n:ContextAssertion) ON (n.embedding)
OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}};
CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS
FOR (n:SourceChunk) ON (n.embedding)
OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}};
