"""Distribution: the gated, reversible publish of statements to writer portals
(ingestion PRD Stage C, infra PRD §Phase 2).

`gate.compute_gate` proves a batch is safe to send; `publish.distribute_batch`
enforces the gate, then creates Distribution rows with cadence de-dup and
supersede-on-reingest. Nothing here moves money — it publishes documents.
"""
