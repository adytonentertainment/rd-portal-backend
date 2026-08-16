"""Client-list ingestion (infra PRD §3.2).

Turns `Client List for Verax.xlsx` into Writer / Contact / WriterContact rows,
name-matches each row to the statement account population, and emits findings
under the same severity model as statement validation. Import is a reviewed,
idempotent diff — never a blind upsert.
"""
