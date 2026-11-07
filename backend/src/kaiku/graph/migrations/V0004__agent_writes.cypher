// Agent writes through MCP (plan 001 §7): idempotency receipts and the new node kinds.
CREATE CONSTRAINT write_receipt_key IF NOT EXISTS
FOR (n:WriteReceipt) REQUIRE n.key IS UNIQUE;
CREATE CONSTRAINT flag_id_unique IF NOT EXISTS
FOR (n:Flag) REQUIRE n.flag_id IS UNIQUE;
CREATE INDEX gap_status IF NOT EXISTS FOR (n:KnowledgeGap) ON (n.status);
