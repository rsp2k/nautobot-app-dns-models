# AAAA Record Model

The AAAA Record model is used to represent IPv6 address records in DNS. It maps a hostname to an IPv6 address.

- `name` (string): FQDN of the record, without TLD.
- `zone` (DNSZoneModel): The DNS zone this record belongs to.
- `ttl` (integer): Time to live for the record.
- `description` (string): Description of the record.
- `comment` (string): Comment for the record.
- `ip_address` (IPAddress): IPv6 address for the record (AAAA records must use IPv6).

+++ 2.0.0
    `address` field in AAAA Record is now `ip_address`

+++ 2.1.2 "Bitemporal records"

    On PostgreSQL deployments this record carries `valid_during`, `recorded_during`, and `entry_id` columns. Calling `save()` after mutating a tracked field rotates the belief log instead of editing in place — the prior row's `recorded_during` window is closed and a successor with a fresh `entry_id` is inserted. The default manager returns the *current* belief only; use `Model.all_versions.as_of(<dt>)` for point-in-time replay. Uniqueness on the natural key applies to the current belief slice only.

    See [Bitemporal Records](../user/feature_bitemporal.md) for the full model, query API, and migration notes. MySQL deployments retain the previous schema and behavior.
