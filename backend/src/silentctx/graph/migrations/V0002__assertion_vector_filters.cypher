// Recreate the assertion vector index as a filtering index (spike BE-1.2, Neo4j 2026.10.1):
// WHERE inside SEARCH only works on properties declared with WITH [...], and a WHERE
// placed before or after SEARCH filters AFTER the top-k, which silently loses results.
DROP INDEX assertion_embedding IF EXISTS;
CREATE VECTOR INDEX assertion_embedding IF NOT EXISTS
FOR (n:ContextAssertion) ON (n.embedding)
WITH [n.status]
OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}};
