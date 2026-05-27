# MX Record Model

+++ 2.1.2 "Bitemporal records"

    On PostgreSQL deployments this record carries `valid_during`, `recorded_during`, and `entry_id` columns. Calling `save()` after mutating a tracked field rotates the belief log instead of editing in place — the prior row's `recorded_during` window is closed and a successor with a fresh `entry_id` is inserted. The default manager returns the *current* belief only; use `Model.all_versions.as_of(<dt>)` for point-in-time replay. Uniqueness on the natural key applies to the current belief slice only.

    See [Bitemporal Records](../user/feature_bitemporal.md) for the full model, query API, and migration notes. MySQL deployments retain the previous schema and behavior.
