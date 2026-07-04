"""Cross-cutting primitives shared by every module: errors, structured logging,
identifiers, and the domain enums (terminal task states, roles) that the schema
and the module interfaces both depend on.

``common`` depends on nothing else in ``acp`` — it is the leaf of the import
graph, so it can never introduce a cycle.
"""
