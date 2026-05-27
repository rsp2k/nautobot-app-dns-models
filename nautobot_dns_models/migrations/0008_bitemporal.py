"""
Migration 0008 -- adds the bitemporal columns and constraints.

Postgres-only. On MySQL this migration is a no-op (records as applied with no
schema changes); MySQL deployments keep their existing non-bitemporal schema.

What it does on Postgres
------------------------

For each of the 10 bitemporal-enabled tables:

1. Add three columns: ``valid_during``, ``recorded_during`` (both
   ``tstzrange``), and ``entry_id`` (``uuid``).
2. Backfill existing rows: ``valid_during = recorded_during = [created, +inf)``
   so every pre-bitemporal row becomes the open belief window for its natural
   key, with a fresh ``entry_id``.
3. Drop the existing ``UNIQUE`` constraints on the natural key (they would
   reject every successor row after an amend).
4. Install partial unique indexes ``WHERE upper(recorded_during) IS NULL`` so
   at most one current-belief row per natural key exists.
5. Install GiST exclusion constraints (requires ``btree_gist``) to forbid
   overlapping belief windows for the same natural key in the closed-history
   tail -- catches double-write race conditions.

Why a single migration for all 10 tables
----------------------------------------

These changes form one logical edit ("turn on bitemporality"). Splitting
across 10 migrations would mean partial states where some tables are
bitemporal and others aren't, which the ``BitemporalMixin``'s
``base_manager_name`` does not gracefully handle. One migration, one
transactional flip.
"""

from django.db import migrations

from nautobot_dns_models.bitemporal import BITEMPORAL_ENABLED


# --- Per-table descriptors ---------------------------------------------------
# (table_name, natural_key_column_list, prior_unique_constraint_names)
#
# The prior_unique_constraint_names tail is informational; we drop unique
# constraints by emitting raw SQL against pg_constraint, since the names
# Django chose are version-dependent.

BITEMPORAL_TABLES = [
    ("nautobot_dns_models_dnszone", ["name", "dns_view_id"]),
    ("nautobot_dns_models_dnsregistration", ["dns_registrar_id", "dns_zone_id"]),
    ("nautobot_dns_models_nsrecord", ["name", "server", "zone_id"]),
    ("nautobot_dns_models_arecord", ["name", "ip_address_id", "zone_id"]),
    ("nautobot_dns_models_aaaarecord", ["name", "ip_address_id", "zone_id"]),
    ("nautobot_dns_models_cnamerecord", ["name", "alias", "zone_id"]),
    ("nautobot_dns_models_mxrecord", ["name", "mail_server", "zone_id"]),
    ("nautobot_dns_models_txtrecord", ["name", "text", "zone_id"]),
    ("nautobot_dns_models_ptrrecord", ["name", "ptrdname", "zone_id"]),
    ("nautobot_dns_models_srvrecord", ["name", "target", "port", "zone_id"]),
]


# Models referenced in `state_operations`. Listed as (django_model_name,
# old_unique_together_set) so we can update Django's state to reflect the
# dropped unique_together without touching the schema (the SQL is in
# database_operations).
STATE_MODELS = [
    ("dnszone", {("name", "dns_view")}),
    ("dnsregistration", {("dns_registrar", "dns_zone")}),
    ("nsrecord", {("name", "server", "zone")}),
    ("arecord", {("name", "ip_address", "zone")}),
    ("aaaarecord", {("name", "ip_address", "zone")}),
    ("cnamerecord", {("name", "alias", "zone")}),
    ("mxrecord", {("name", "mail_server", "zone")}),
    ("txtrecord", {("name", "text", "zone")}),
    ("ptrrecord", {("name", "ptrdname", "zone")}),
    ("srvrecord", {("name", "target", "port", "zone")}),
]


def _short_name(table: str) -> str:
    """Return the short DB name (suffix after the app prefix) for index naming."""
    return table.removeprefix("nautobot_dns_models_")


def apply_bitemporal(apps, schema_editor):
    """Add columns, backfill, drop old uniques, install new partial-unique + exclusion."""
    if schema_editor.connection.vendor != "postgresql":
        return

    cursor = schema_editor.connection.cursor()

    # btree_gist gives us scalar-equality operators (= on uuid, text, etc.)
    # alongside the && operator on tstzrange -- prerequisite for the
    # ExclusionConstraint that prevents overlapping belief windows.
    cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")

    for table, natural_key in BITEMPORAL_TABLES:
        short = _short_name(table)

        # 1. Add columns. nullable initially so the backfill can run.
        cursor.execute(
            f"""
            ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS valid_during    tstzrange,
                ADD COLUMN IF NOT EXISTS recorded_during tstzrange,
                ADD COLUMN IF NOT EXISTS entry_id        uuid;
            """
        )

        # 2. Backfill. Use `created` as the lower bound. Each existing row
        # becomes the *current* belief for its natural key (upper=+inf).
        cursor.execute(
            f"""
            UPDATE {table}
            SET valid_during    = tstzrange(COALESCE(created, now()), NULL, '[)'),
                recorded_during = tstzrange(COALESCE(created, now()), NULL, '[)'),
                entry_id        = gen_random_uuid()
            WHERE valid_during IS NULL OR recorded_during IS NULL OR entry_id IS NULL;
            """
        )

        # 3. Tighten -- now require non-null.
        cursor.execute(
            f"""
            ALTER TABLE {table}
                ALTER COLUMN valid_during    SET NOT NULL,
                ALTER COLUMN recorded_during SET NOT NULL,
                ALTER COLUMN entry_id        SET NOT NULL;
            """
        )

        # 4. Drop the prior unique constraint on the natural key. Django's
        # constraint names are version-dependent, so query pg_constraint by
        # the column set rather than by name.
        #
        # NOTE: pg_attribute.attname is type `name`, not `text`. array_agg
        # of `name` values yields `name[]`, and Postgres has no implicit
        # cast between `name[]` and `text[]` (only at the scalar level).
        # We must cast attname::text BEFORE aggregating to make the array
        # comparison work.
        cols_array_sql = "ARRAY[" + ", ".join(f"'{c}'" for c in natural_key) + "]::text[]"
        cursor.execute(
            f"""
            DO $$
            DECLARE
                con_name text;
            BEGIN
                FOR con_name IN
                    SELECT c.conname
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = '{table}'
                      AND c.contype = 'u'
                      AND (
                        SELECT array_agg(a.attname::text ORDER BY a.attname::text)
                        FROM unnest(c.conkey) AS k(attnum)
                        JOIN pg_attribute a
                          ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                      ) = (
                        SELECT array_agg(x ORDER BY x)
                        FROM unnest({cols_array_sql}) AS x
                      )
                LOOP
                    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', con_name);
                END LOOP;
            END$$;
            """
        )

        # 5. Partial unique index -- at most one current belief per natural key.
        nk_cols = ", ".join(natural_key)
        cursor.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {short}_current_unique
                ON {table} ({nk_cols})
                WHERE upper(recorded_during) IS NULL;
            """
        )

        # 6. GiST exclusion -- no two CURRENT belief rows for the same natural
        # key can have overlapping recorded_during. The partial-WHERE clause
        # restricts this to the open-window slice; closed historical rows are
        # allowed to overlap each other freely (and they often will, since
        # successive amends produce closely-spaced but distinct windows).
        #
        # DROP-then-ADD makes this step idempotent. If a prior failed run of
        # this migration left exclusion constraints in place (CREATE EXTENSION
        # can implicitly commit on some Postgres versions, breaking the outer
        # rollback), the retry would otherwise fail with "constraint already
        # exists." Dropping first guarantees the retry always succeeds.
        gist_expressions = ", ".join(f"{c} WITH =" for c in natural_key)
        cursor.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {short}_no_belief_overlap;"
        )
        cursor.execute(
            f"""
            ALTER TABLE {table}
                ADD CONSTRAINT {short}_no_belief_overlap
                EXCLUDE USING gist (
                    {gist_expressions},
                    recorded_during WITH &&
                ) WHERE (upper(recorded_during) IS NULL);
            """
        )


def unapply_bitemporal(apps, schema_editor):
    """Reverse: drop new constraints/indexes/columns, restore the prior unique."""
    if schema_editor.connection.vendor != "postgresql":
        return

    cursor = schema_editor.connection.cursor()
    for table, natural_key in BITEMPORAL_TABLES:
        short = _short_name(table)
        cursor.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {short}_no_belief_overlap;"
        )
        cursor.execute(f"DROP INDEX IF EXISTS {short}_current_unique;")
        # Restore the natural-key unique constraint that existed pre-bitemporal.
        nk_cols = ", ".join(natural_key)
        cursor.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {short}_natural_unique UNIQUE ({nk_cols});"
        )
        cursor.execute(
            f"""
            ALTER TABLE {table}
                DROP COLUMN IF EXISTS valid_during,
                DROP COLUMN IF EXISTS recorded_during,
                DROP COLUMN IF EXISTS entry_id;
            """
        )


def _build_state_operations():
    """
    Build the `state_operations` block.

    Only emitted on Postgres -- on MySQL, the model class doesn't declare
    bitemporal fields (the `if BITEMPORAL_ENABLED:` guard in
    `BitemporalMixin`), so adding them to Django's state would diverge from
    the class definition and trip up future autodetect.
    """
    if not BITEMPORAL_ENABLED:
        return []

    # Local imports -- only valid on Postgres-installed environments.
    import uuid as _uuid

    from django.contrib.postgres.fields import DateTimeRangeField
    from django.db import models as _m

    from nautobot_dns_models.bitemporal import _open_belief_window

    field_defs = [
        ("valid_during", DateTimeRangeField(default=_open_belief_window)),
        ("recorded_during", DateTimeRangeField(default=_open_belief_window)),
        ("entry_id", _m.UUIDField(default=_uuid.uuid4, editable=False)),
    ]

    ops = []
    for model_name, _old_unique in STATE_MODELS:
        for fname, fval in field_defs:
            ops.append(migrations.AddField(model_name=model_name, name=fname, field=fval))
        ops.append(migrations.AlterUniqueTogether(name=model_name, unique_together=set()))

    return ops


class Migration(migrations.Migration):
    dependencies = [
        ("nautobot_dns_models", "0007_dnsregistrar_dnsregistration"),
    ]

    # SeparateDatabaseAndState lets us emit raw SQL (database_operations) while
    # telling Django's model state about the new fields and dropped uniques
    # (state_operations). On MySQL, state_operations is empty and the
    # RunPython is a no-op -- migration completes with no schema changes.
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(apply_bitemporal, reverse_code=unapply_bitemporal),
            ],
            state_operations=_build_state_operations(),
        ),
    ]
