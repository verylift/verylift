"""Models for the generic workout-CSV import (#11).

Per-tracker alias data (formerly HevyLiftAlias, StrongLiftAlias) now lives in
the unified core.models.LiftAlias table (source="hevy"/"strong") -- see
core.lift_resolution for the shared resolution chain both CSV importers use.
"""
