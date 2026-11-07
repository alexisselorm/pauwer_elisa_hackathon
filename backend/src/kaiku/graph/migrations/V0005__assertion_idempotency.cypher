// The single write path (BE-1.5): one assertion per source event + statement hash.
// Gold rows have no key, and uniqueness ignores nulls.
CREATE CONSTRAINT assertion_idempotency_key IF NOT EXISTS
FOR (n:ContextAssertion) REQUIRE n.idempotency_key IS UNIQUE;
