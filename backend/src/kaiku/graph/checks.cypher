// The temporal invariants (plan 001 §3.3 R1-R7, §6.1 V5): mock_data/scripts/verify_temporal.py
// as Cypher, run on the graph itself. Loaded by graph/checks.py.
//
// Each check starts with a header line, "// check: <name> [<anchor>]", and returns one row
// per violation with an `id` column naming what is wrong. No rows means the graph is clean.
//
// Parameters:
//   $ids  null for the whole graph, or the keys to check: assertion_id for [assertion],
//         event_id for [event], thread_id for [thread]. A [graph] check always reads the
//         whole graph and is not run inside a write.
//   $now  the instant "past" is measured against (null: the database clock). Only the
//         status checks use it, which is why they are [graph] checks.
//
// Windows are half-open [valid_from, valid_to), a null valid_to is open-ended. A successor
// counts only while it is live (not retracted): a retracted successor hands its
// predecessor back, which keeps its stored valid_to on record (R6, R10).

// ---------------------------------------------------------------------------- validity windows

// check: node_window_order [graph]
// A window must not be empty or reversed.
MATCH (n) WHERE n.valid_from IS NOT NULL AND n.valid_to IS NOT NULL
AND n.valid_to <= n.valid_from
RETURN coalesce(n.entity_id, n.assertion_id, n.snapshot_id, n.record_id, elementId(n)) AS id

// check: relationship_window_order [graph]
MATCH ()-[r]->() WHERE r.valid_from IS NOT NULL AND r.valid_to IS NOT NULL
AND r.valid_to <= r.valid_from
RETURN type(r) + ':' + elementId(r) AS id

// check: edge_within_endpoints [graph]
// A topology edge can't start before, or after the end of, either endpoint.
MATCH (a:Entity)-[r:IS_MODEL|CONNECTS|LINKED]->(b:Entity)
WHERE r.valid_from < a.valid_from OR r.valid_from < b.valid_from
OR r.valid_from >= coalesce(a.valid_to, datetime('9999-12-31T00:00:00Z'))
OR r.valid_from >= coalesce(b.valid_to, datetime('9999-12-31T00:00:00Z'))
RETURN a.entity_id + '-' + type(r) + '->' + b.entity_id AS id

// check: snapshot_within_device [graph]
// As-running snapshots of one device form a non-overlapping chain inside its life.
MATCH (s:RunningSnapshot)-[:SNAPSHOT_OF]->(d:Device)
WHERE s.valid_from < d.valid_from
OR s.valid_from >= coalesce(d.valid_to, datetime('9999-12-31T00:00:00Z'))
RETURN s.snapshot_id AS id

// check: snapshot_overlap [graph]
MATCH (s1:RunningSnapshot)-[:SNAPSHOT_OF]->(d:Device)<-[:SNAPSHOT_OF]-(s2:RunningSnapshot)
WHERE s1.snapshot_id < s2.snapshot_id
AND s1.valid_from < coalesce(s2.valid_to, datetime('9999-12-31T00:00:00Z'))
AND s2.valid_from < coalesce(s1.valid_to, datetime('9999-12-31T00:00:00Z'))
RETURN s1.snapshot_id + '/' + s2.snapshot_id AS id

// check: access_register_overlap [graph]
// The access register is one non-overlapping chain per segment.
MATCH (r1:AccessRecord)-[:FOR]->(s:Segment)<-[:FOR]-(r2:AccessRecord)
WHERE r1.record_id < r2.record_id
AND r1.valid_from < coalesce(r2.valid_to, datetime('9999-12-31T00:00:00Z'))
AND r2.valid_from < coalesce(r1.valid_to, datetime('9999-12-31T00:00:00Z'))
RETURN r1.record_id + '/' + r2.record_id AS id

// check: bound_entity_exists_at_valid_from [graph]
// V3 / R9: an entity the knowledge is bound to exists when the knowledge starts to hold.
// Candidate bindings are exempt: that is how V3 stores a binding it could not confirm.
// Not run inside a write, where V3 asks a person instead of rolling back.
MATCH (a:ContextAssertion)-[b:ABOUT {scope: 'observed_on'}]->(e:Entity)
WHERE NOT coalesce(b.candidate, false)
AND (a.valid_from < e.valid_from
  OR a.valid_from >= coalesce(e.valid_to, datetime('9999-12-31T00:00:00Z')))
RETURN a.assertion_id + '@' + e.entity_id AS id

// ---------------------------------------------------------------------------- status vs time

// check: open_status_past_valid_to [graph]
// A version still believed (or doubted) can't have ended: it should be superseded or expired.
MATCH (a:ContextAssertion)
WHERE coalesce(a.status, 'unverified') IN ['believed_true', 'disputed', 'unverified']
AND a.retracted_at IS NULL AND a.valid_to IS NOT NULL
AND a.valid_to <= coalesce($now, datetime())
AND NOT (EXISTS { (:ContextAssertion)-[:SUPERSEDES]->(a) }
  AND NOT EXISTS { (s:ContextAssertion)-[:SUPERSEDES]->(a) WHERE s.retracted_at IS NULL })
RETURN a.assertion_id AS id

// check: expired_has_ended [graph]
MATCH (a:ContextAssertion {status: 'expired'})
WHERE a.valid_to IS NULL OR a.valid_to > coalesce($now, datetime())
RETURN a.assertion_id AS id

// ---------------------------------------------------------------------------- supersession

// check: supersession_timestamp [assertion]
// R4b: a successor's valid_from is its predecessor's valid_to (the superseded timestamp).
MATCH (s:ContextAssertion)-[:SUPERSEDES]->(p:ContextAssertion)
WHERE ($ids IS NULL OR s.assertion_id IN $ids OR p.assertion_id IN $ids)
AND s.retracted_at IS NULL
AND (p.valid_to IS NULL OR p.valid_to <> s.valid_from)
RETURN s.assertion_id + '>' + p.assertion_id AS id

// check: superseded_has_successor [assertion]
MATCH (p:ContextAssertion {status: 'superseded'})
WHERE ($ids IS NULL OR p.assertion_id IN $ids) AND p.retracted_at IS NULL
AND NOT EXISTS { (s:ContextAssertion)-[:SUPERSEDES]->(p) WHERE s.retracted_at IS NULL }
RETURN p.assertion_id AS id

// check: expired_has_no_successor [assertion]
// Expiry (R4a, R4c) and supersession (R4b) are different endings: an expired version has
// no live successor.
MATCH (a:ContextAssertion {status: 'expired'})
WHERE ($ids IS NULL OR a.assertion_id IN $ids)
AND EXISTS { (s:ContextAssertion)-[:SUPERSEDES]->(a) WHERE s.retracted_at IS NULL }
RETURN a.assertion_id AS id

// check: superseded_by_mirrors_edge [assertion]
// The superseded_by property and the SUPERSEDES edge say the same thing.
MATCH (p:ContextAssertion) WHERE p.superseded_by IS NOT NULL
AND ($ids IS NULL OR p.assertion_id IN $ids)
AND (NOT EXISTS { (:ContextAssertion {assertion_id: p.superseded_by})-[:SUPERSEDES]->(p) }
  OR (p.retracted_at IS NULL AND p.status <> 'superseded'))
RETURN p.assertion_id + '<' + p.superseded_by AS id

// check: supersedes_is_asymmetric [assertion]
MATCH (a:ContextAssertion)-[:SUPERSEDES]->(b:ContextAssertion)
WHERE ($ids IS NULL OR a.assertion_id IN $ids OR b.assertion_id IN $ids)
AND (a = b OR EXISTS { (b)-[:SUPERSEDES]->(a) })
RETURN a.assertion_id + '>' + b.assertion_id AS id

// ---------------------------------------------------------------------------- symmetric links

// check: symmetric_link_stored_once [assertion]
// CONTRADICTS and CORROBORATES are symmetric: one edge per pair, read undirected. A
// self-link, or the same pair stored both ways, makes one side count twice.
MATCH (a:ContextAssertion)-[r:CONTRADICTS|CORROBORATES]->(b:ContextAssertion)
WHERE ($ids IS NULL OR a.assertion_id IN $ids OR b.assertion_id IN $ids)
AND (a = b OR (a.assertion_id < b.assertion_id
  AND EXISTS { MATCH (b)-[r2]->(a) WHERE type(r2) = type(r) }))
RETURN type(r) + ':' + a.assertion_id + '/' + b.assertion_id AS id

// check: disputed_has_contradiction [assertion]
MATCH (a:ContextAssertion {status: 'disputed'})
WHERE ($ids IS NULL OR a.assertion_id IN $ids)
AND NOT EXISTS { (a)-[:CONTRADICTS]-(:ContextAssertion) }
RETURN a.assertion_id AS id

// ---------------------------------------------------------------------------- record time

// check: recorded_at_is_source_event_time [assertion]
// R2: recorded_at is the event_time of the event that completes the assertion, the last
// one it is DERIVED_FROM. An agent proposal is recorded when proposed, so only "not before
// its evidence" applies to it.
MATCH (a:ContextAssertion)-[:DERIVED_FROM]->(ev:Event)
WHERE $ids IS NULL OR a.assertion_id IN $ids
WITH a, max(ev.event_time) AS last
WHERE a.recorded_at IS NULL OR CASE WHEN a.origin = 'agent_proposal'
  THEN a.recorded_at < last ELSE a.recorded_at <> last END
RETURN a.assertion_id AS id

// check: retracted_after_recorded [assertion]
// R6: a version can't be retracted before it was recorded.
MATCH (a:ContextAssertion) WHERE a.retracted_at IS NOT NULL
AND ($ids IS NULL OR a.assertion_id IN $ids)
AND a.retracted_at < a.recorded_at
RETURN a.assertion_id AS id

// ---------------------------------------------------------------------------- events

// check: event_has_one_event_time [event]
// R1: an event has a single event_time from its platform. A record time stored beside it
// must be the same instant (record time = event time).
MATCH (e:Event) WHERE ($ids IS NULL OR e.event_id IN $ids)
AND (NOT e.event_time IS :: ZONED DATETIME NOT NULL
  OR (e.record_time IS NOT NULL AND e.record_time <> e.event_time)
  OR (e.recorded_at IS NOT NULL AND e.recorded_at <> e.event_time))
RETURN coalesce(e.event_id, elementId(e)) AS id

// check: event_has_no_validity [event]
// R1: events are as-observed and immutable. Validity belongs to context, not to events.
MATCH (e:Event) WHERE ($ids IS NULL OR e.event_id IN $ids)
AND (e.valid_from IS NOT NULL OR e.valid_to IS NOT NULL)
RETURN coalesce(e.event_id, elementId(e)) AS id

// check: event_ends_after_it_starts [event]
// A device event's end, and an alarm's clearing, come after it started.
MATCH (e:Event) WHERE ($ids IS NULL OR e.event_id IN $ids)
AND ((e.end_time IS NOT NULL AND e.end_time <= e.event_time)
  OR (e.cleared_at IS NOT NULL AND e.cleared_at < e.event_time))
RETURN e.event_id AS id

// check: event_within_device [event]
// An observed event happens while its device exists.
MATCH (e:Event)-[:ON]->(d:Device)
WHERE ($ids IS NULL OR e.event_id IN $ids)
AND (e.event_time < d.valid_from
  OR e.event_time > coalesce(d.valid_to, datetime('9999-12-31T00:00:00Z')))
RETURN e.event_id AS id

// check: thread_strictly_increasing [thread]
// R7: messages in a thread are strictly increasing in event_time. Equal instants are
// ordered only by a platform sequence that differs (Slack ts has microseconds).
MATCH (m:Event)-[:IN_THREAD]->(t:Thread)
WHERE $ids IS NULL OR t.thread_id IN $ids
WITH t, m ORDER BY m.event_time, m.platform_seq, m.event_id
WITH t, collect(m) AS ms
UNWIND range(1, size(ms) - 1) AS i
WITH t, ms[i - 1] AS a, ms[i] AS b
WHERE a.event_time = b.event_time
AND (a.platform_seq IS NULL OR b.platform_seq IS NULL OR a.platform_seq = b.platform_seq)
RETURN t.thread_id + ': ' + a.event_id + ' / ' + b.event_id AS id
