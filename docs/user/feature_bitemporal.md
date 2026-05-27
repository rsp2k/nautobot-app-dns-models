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

## Mutation contract — `save()` vs `amend()`

| Call | What happens | When to use |
|------|--------------|-------------|
| `obj.save()` | Standard Django in-place UPDATE. The pk is stable across the call. No new belief row is created. | Edits via Nautobot UI/REST, normal Django flow, anywhere the framework expects pk stability. |
| `obj.amend(field=new_value, ...)` | Sequenced amend: closes prior `recorded_during` window, INSERTs a successor row with a fresh `entry_id`, and rebinds `self` to the successor. | Ingest pipelines, scanner reconciliation, anywhere you want a new audit-trail entry. |

The split matters because Nautobot's UI views, REST `PATCH`/`PUT`
endpoints, and the testing framework all assume `save()` is in-place.
Routing belief-log mutations through an explicit `amend()` keeps both
contracts intact.

Example — promoter idiom (the pattern used by `nautobot-app-scanner`):

```python
obj, created = ARecord.objects.get_or_create(
    name=name, ip_address=ip, zone=zone,
    defaults={"_ttl": ttl, "description": desc},
)
if not created and wire_data_differs(obj, scan):
    # Real-world change observed -- rotate the belief log.
    obj.amend(_ttl=scan.ttl, description=scan.description)
    # obj.pk is now the successor's pk; obj.entry_id is fresh.
```

The closure UPDATE on the prior row uses `queryset.update()` directly
(bypassing `save()` to avoid ticking `last_updated`), so the audit
trail's "when did the prior belief become superseded" timestamp is
honest.

On MySQL or other non-Postgres backends, `amend()` falls back to a
plain in-place UPDATE -- there's no belief log to rotate.

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
