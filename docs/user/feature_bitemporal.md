# Bitemporal Records

The app stores DNS records, zones, and registrations along **two time axes**
so you can answer questions like _"what did we believe about this record on
March 15?"_ without depending on Nautobot's ObjectChange log.

## The two axes

| Axis | Field | Question it answers |
|------|-------|---------------------|
| Valid time | `valid_during` | When was this fact true in the real world? |
| Recording time | `recorded_during` | When did Nautobot believe this fact? |

Each row also has an `entry_id` — a stable UUID for that specific belief row,
distinct from the model's primary key. The PK changes every time the row is
amended; `entry_id` is the durable handle for one slice of belief.

A typical lifecycle:

1. **Create A record** at 10:00 → row 1 with `recorded_during = [10:00, ∞)`.
2. **Change its IP** at 14:00 → row 1 closes to `[10:00, 14:00)`, row 2 opens
   with the new IP and `recorded_during = [14:00, ∞)`. The same `valid_during`
   carries over (the *fact* was always true, we just changed our belief).
3. **Compliance audit** asks "what did we have at 12:30?" → query
   `ARecord.all_versions.as_of("2026-03-15T12:30Z")` returns row 1.

## What's bitemporal

These models gain two-axis history:

- `DNSZone`
- `DNSRegistration`
- `NSRecord`, `ARecord`, `AAAARecord`, `CNAMERecord`, `MXRecord`,
  `TXTRecord`, `PTRRecord`, `SRVRecord`

`DNSView`, `DNSRegistrar`, and `DNSViewPrefixAssignment` are config-shaped
and remain single-row models.

## How "save()" behaves

When you edit a bitemporal row and call `.save()`:

- If you only touched non-tracked fields (`_custom_field_data`, tags), no
  new row is created.
- If any **tracked** field (the model's natural-key fields plus everything
  the model considers business state) changed, the prior row's
  `recorded_during.upper` is closed and a new row is inserted with the new
  values. The Python instance is then rebound to the successor row, so
  callers see a seamless `.pk` reference after save.

This is the standard "sequenced amend" pattern — every belief is an INSERT;
the only UPDATE is the timestamp closure, which goes through the queryset
manager directly (bypassing `save()` and `last_updated` ticks) to keep the
audit trail honest.

## Querying

```python
# Current beliefs (default manager filters to upper(recorded_during) IS NULL)
ARecord.objects.filter(zone__name="example.com")

# Every belief row ever recorded (use this for forensic queries)
ARecord.all_versions.filter(zone__name="example.com")

# Point-in-time replay
ARecord.all_versions.as_of("2026-03-15T12:00:00Z")

# History of one row (matched by natural key)
my_record.history()
```

## REST API

Two extensions, both Postgres-only:

- `GET /api/plugins/dns/<resource>/?as_of=2026-03-15T12:00:00Z` —
  returns the belief state at that instant.
- `GET /api/plugins/dns/<resource>/<id>/history/` — every belief row that
  shares this row's natural key, oldest first.

## UI

Each bitemporal object has a per-row history page at
`/plugins/dns/bitemporal/<model-slug>/<pk>/history/` showing the audit
trail in chronological order. The current-belief row is marked
**current**; superseded rows show the window in which they were the
authoritative belief.

## Database support

Bitemporal storage uses PostgreSQL's `tstzrange` type plus GiST
exclusion constraints (`btree_gist`). On MySQL deployments the
0008 migration runs as a no-op — the bitemporal columns are not added,
the runtime mixin detects `connection.vendor` and falls back to
plain save semantics, and the REST extensions return 400 / list
endpoints ignore `?as_of=`.

## Migration is forward-only after first amend

The 0008 migration is technically reversible (its `unapply_bitemporal`
restores the prior unique constraint and drops the bitemporal columns)
— **but only against an unamended bitemporal state**. Once any row
has been amended via `obj.save()`, the natural key has two physical
rows that are only disambiguated by their `recorded_during` windows.
Dropping the bitemporal columns leaves those rows visible as
duplicate naturals, and restoring the prior `UNIQUE` constraint
will fail.

If you need to roll back after amends have occurred, restore from
backup or manually delete the superseded rows before reversing the
migration:

```sql
-- Identify and remove superseded rows before rollback
DELETE FROM nautobot_dns_models_<table>
WHERE upper(recorded_during) IS NOT NULL;
```

If you're running on MySQL today and need bitemporal features, switch
the deployment to PostgreSQL and re-run migrations. The schema
migration tooling is not included — Nautobot's own migration
guidance applies (`nautobot-server dumpdata` + reload).

## Why two axes instead of just an audit log

Nautobot already records change history via `ObjectChange` — that's
the **recording-time** axis. What it lacks is **valid time**: the
notion that a fact has its own truth window independent of when
Nautobot learned about it. For DNS specifically, this matters when:

- a registrar reports an expiration date that was already true last
  month (late-arriving fact),
- you backdate a record entry because the actual DNS publication
  happened before someone entered it in Nautobot,
- a compliance auditor asks "was MX 10 mail.example.com in effect
  during the incident?" — answerable via `as_of()` without
  reconstructing from the change log.

The full architectural rationale lives at
<https://l2trace.warehack.ing/explanation/bitemporality/>.
