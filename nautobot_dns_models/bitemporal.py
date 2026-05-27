"""
Bitemporal mixin for Nautobot DNS Models.

Adds two time axes to selected models:

- ``valid_during``    -- when the fact was true in the world.
- ``recorded_during`` -- when we (Nautobot) believed it.

Together they enable point-in-time replay ("what did we believe about zone X
on 2026-03-15?") and unambiguous reconciliation with late-arriving data from
registrars or external probes.

The implementation follows the sequenced-amend pattern: every "update" is
actually two writes inside a transaction -- close the prior belief window,
then insert a new row carrying the new field values with a fresh ``entry_id``.
Foreign keys still point at the natural key (current belief) so the rest of
the Nautobot stack (UI, REST, GraphQL, webhooks) behaves normally.

PostgreSQL-only. The migration that physically adds the columns is a no-op on
non-Postgres backends; the mixin's runtime methods detect ``vendor != 'postgresql'``
and fall back to plain save semantics so MySQL deployments keep working
without the bitemporal features.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


def _engine_is_postgres(engine: str) -> bool:
    return "postgresql" in (engine or "").lower()


def _default_db_is_postgres() -> bool:
    """Return True if the default DB connection uses a Postgres engine.

    Resolved at import time so the rest of the module can branch class-body
    declarations and migration operations on a single boolean.
    """
    default = settings.DATABASES.get("default", {}) if hasattr(settings, "DATABASES") else {}
    return _engine_is_postgres(default.get("ENGINE", ""))


# Module-level constant -- safe because Django settings are loaded before
# app modules are imported. Conditioned on the *default* connection because
# Nautobot apps only support a single primary DB.
BITEMPORAL_ENABLED = _default_db_is_postgres()


# Fields excluded from change detection. Modifications to these fields alone
# do NOT trigger a sequenced amend (no new belief row is created).
DEFAULT_UNTRACKED_FIELDS = frozenset(
    {
        "id",
        "created",
        "last_updated",
        "_custom_field_data",
        "valid_during",
        "recorded_during",
        "entry_id",
    }
)


if BITEMPORAL_ENABLED:
    # Range type imports are wrapped because they only resolve when psycopg2
    # is installed (it is, when running on Postgres). Importing them on a
    # MySQL-only deployment would error on `psycopg2.extras`.
    from django.contrib.postgres.fields import DateTimeRangeField
    from psycopg2.extras import DateTimeTZRange

    def _open_belief_window() -> "DateTimeTZRange":
        """Return ``[now(), ∞)`` -- the canonical open belief window."""
        return DateTimeTZRange(lower=timezone.now(), upper=None, bounds="[)")
else:  # pragma: no cover -- exercised only on MySQL CI
    DateTimeRangeField = None  # type: ignore[assignment]
    DateTimeTZRange = None  # type: ignore[assignment]

    def _open_belief_window():
        return None


class BitemporalQuerySet(models.QuerySet):
    """QuerySet with valid-time and recording-time query helpers."""

    def current(self) -> "BitemporalQuerySet":
        """Restrict to rows whose belief window is still open (``upper IS NULL``).

        This is the "what does Nautobot currently believe?" view. On MySQL it
        degrades to a no-op since there's only one row per natural key anyway.
        """
        if not BITEMPORAL_ENABLED:
            return self
        return self.filter(recorded_during__upper_inf=True)

    def as_of(self, dt) -> "BitemporalQuerySet":
        """Restrict to the rows that were current at ``dt``.

        Useful for compliance forensics: ``ARecord.all_versions.as_of(incident_time)``
        returns exactly the rows Nautobot would have shown at that instant.
        """
        if not BITEMPORAL_ENABLED:
            return self
        return self.filter(recorded_during__contains=dt)

    def all_versions(self) -> "BitemporalQuerySet":
        """Convenience: include every belief row regardless of recording window."""
        return self.all()


class BitemporalManager(models.Manager.from_queryset(BitemporalQuerySet)):
    """Default manager that filters to the *current* belief row.

    Diverges from the bitemporal-rule convention of "``all()`` means all rows"
    on purpose: Nautobot's viewsets, GraphQL nodes, webhook dispatchers and
    template tags all assume the canonical manager returns canonical objects.
    Use ``Model.all_versions.all()`` for the unfiltered view.
    """

    def get_queryset(self) -> BitemporalQuerySet:
        qs = super().get_queryset()
        if BITEMPORAL_ENABLED:
            return qs.filter(recorded_during__upper_inf=True)
        return qs


class AllVersionsManager(models.Manager.from_queryset(BitemporalQuerySet)):
    """Non-default manager that returns every belief row including amended-away ones."""


class BitemporalMixin(models.Model):
    """
    Abstract mixin that gives a model two time axes plus sequenced-amend semantics.

    Concrete models inheriting this gain three columns (on Postgres):

    - ``valid_during``: when the fact was/is true in the world
    - ``recorded_during``: when this row was the current belief
    - ``entry_id``: distinguishes successive belief rows about the same natural key

    Calls to ``.save()`` on an existing row trigger a sequenced amend: the
    prior row's ``recorded_during.upper`` is closed and a new row is inserted
    carrying the changed field values with a fresh ``entry_id``. The Python
    instance is mutated to point at the new row so callers can keep using it
    transparently.
    """

    # Subclasses may override to include/exclude specific fields from change
    # detection. By default everything not in DEFAULT_UNTRACKED_FIELDS counts.
    BITEMPORAL_UNTRACKED_FIELDS: frozenset = DEFAULT_UNTRACKED_FIELDS

    if BITEMPORAL_ENABLED:
        valid_during = DateTimeRangeField(
            default=_open_belief_window,
            help_text="Wall-clock window when the fact was true in the world.",
        )
        recorded_during = DateTimeRangeField(
            default=_open_belief_window,
            help_text="Window during which this row was Nautobot's current belief.",
        )
        entry_id = models.UUIDField(
            default=uuid.uuid4,
            editable=False,
            help_text="Stable identifier for this specific belief row (distinct from id).",
        )

    objects = BitemporalManager()
    all_versions = AllVersionsManager()

    class Meta:
        abstract = True
        base_manager_name = "all_versions"

    # ------------------------------------------------------------------ helpers

    @classmethod
    def from_db(cls, db, field_names, values):
        """Snapshot tracked-field values at load time for change detection.

        Stashing the DB state on the instance avoids an extra SELECT on each
        save() to figure out which fields changed.
        """
        instance = super().from_db(db, field_names, values)
        instance._bitemporal_db_state = dict(zip(field_names, values))
        return instance

    def _tracked_field_names(self) -> Iterable[str]:
        """Concrete field names that count for change detection."""
        untracked = set(self.BITEMPORAL_UNTRACKED_FIELDS)
        for field in self._meta.concrete_fields:
            if field.name in untracked:
                continue
            # FK columns -- compare the *_id (attname) to avoid lazy-loading the
            # related object.
            yield field.attname

    def _has_tracked_changes(self) -> bool:
        """Compare in-memory state vs DB state on tracked fields."""
        db_state = getattr(self, "_bitemporal_db_state", None)
        if db_state is None:
            # Object was constructed without going through from_db (e.g. manual
            # instantiation after refresh_from_db with deferred fields).
            # Fall back to a single SELECT for the prior row.
            type_ = type(self)
            try:
                fresh = type_.all_versions.get(pk=self.pk)
            except type_.DoesNotExist:
                return True
            db_state = {f.attname: getattr(fresh, f.attname) for f in self._meta.concrete_fields}
            self._bitemporal_db_state = db_state

        for attname in self._tracked_field_names():
            if getattr(self, attname) != db_state.get(attname):
                return True
        return False

    # --------------------------------------------------------------------- save

    def save(self, *args, **kwargs):
        """Persist with sequenced-amend semantics on update.

        - New rows: stamp a fresh ``entry_id`` and open belief window, then
          plain INSERT.
        - Existing rows with no tracked-field changes: plain UPDATE (e.g. tag
          edits, custom_field changes -- nothing that warrants a new belief).
        - Existing rows with tracked changes (on Postgres): close prior
          ``recorded_during`` via raw UPDATE (bypassing ``save()`` and the
          auto-bump of ``last_updated``), insert a new row carrying the
          changed values, then mutate ``self`` to point at the new row.
        - MySQL / non-Postgres: plain save, always.
        """
        # New row, or non-Postgres backend -- standard save path.
        if self._state.adding or not BITEMPORAL_ENABLED:
            self._initialize_bitemporal_fields_if_needed()
            return super().save(*args, **kwargs)

        # In-progress amend? Skip recursion guard.
        if getattr(self, "_bitemporal_amend_in_progress", False):
            return super().save(*args, **kwargs)

        # Explicit closure UPDATE (internal); just save normally.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and set(update_fields).issubset({"recorded_during"}):
            return super().save(*args, **kwargs)

        if not self._has_tracked_changes():
            return super().save(*args, **kwargs)

        # Sequenced amend.
        self._sequenced_amend(*args, **kwargs)

    def _initialize_bitemporal_fields_if_needed(self) -> None:
        """Make sure recorded_during and valid_during are set on first INSERT."""
        if not BITEMPORAL_ENABLED:
            return
        if getattr(self, "recorded_during", None) is None:
            self.recorded_during = _open_belief_window()
        if getattr(self, "valid_during", None) is None:
            # By default, valid time tracks recording time on initial insert.
            # Ingest pipelines that backdate facts should set valid_during
            # explicitly before save().
            self.valid_during = self.recorded_during
        if not getattr(self, "entry_id", None):
            self.entry_id = uuid.uuid4()

    @transaction.atomic
    def _sequenced_amend(self, *args, **kwargs) -> None:
        """Close prior belief window, insert successor, rebind ``self``."""
        type_ = type(self)
        prior_pk = self.pk
        prior_recorded_during = self._bitemporal_db_state.get("recorded_during")
        prior_valid_during = self._bitemporal_db_state.get("valid_during")
        now = timezone.now()

        # 1. Close the prior row. Use queryset.update() to bypass save() (so we
        # don't trigger another amend) and to avoid ticking last_updated.
        type_.all_versions.filter(pk=prior_pk).update(
            recorded_during=DateTimeTZRange(
                lower=prior_recorded_during.lower if prior_recorded_during else None,
                upper=now,
                bounds="[)",
            )
        )

        # 2. Insert the successor as a fresh row carrying the in-memory values.
        # If the caller hasn't explicitly updated valid_during, inherit it from
        # the prior row -- the fact's wall-clock truth window hasn't changed,
        # only our belief about it.
        if self.valid_during == prior_valid_during or self.valid_during is None:
            self.valid_during = prior_valid_during
        self.pk = None
        self.id = None  # for UUID PKs Django assigns a new one on save()
        self.entry_id = uuid.uuid4()
        self.recorded_during = DateTimeTZRange(lower=now, upper=None, bounds="[)")
        self._state.adding = True
        self._bitemporal_amend_in_progress = True
        try:
            super().save(*args, **kwargs)
        finally:
            self._bitemporal_amend_in_progress = False

        # Refresh the snapshot so subsequent saves on the same Python instance
        # compare against the just-written row.
        self._bitemporal_db_state = {
            f.attname: getattr(self, f.attname) for f in self._meta.concrete_fields
        }

    # -------------------------------------------------------- history accessors

    def history(self) -> models.QuerySet:
        """Every belief row that shares this row's natural-key identity.

        Default implementation matches on the model-declared natural-key
        fields (``BITEMPORAL_NATURAL_KEY``). Subclasses may override for
        more complex natural keys.
        """
        natural_key = getattr(type(self), "BITEMPORAL_NATURAL_KEY", None)
        if not natural_key:
            return type(self).all_versions.filter(pk=self.pk)
        criteria = {field: getattr(self, field) for field in natural_key}
        return type(self).all_versions.filter(**criteria).order_by("recorded_during")


# ---------------------------------------------------------------------- helpers
# Used by migrations to safely re-discover whether to run schema operations.


def schema_editor_is_postgres(schema_editor) -> bool:
    """Return True if the schema editor's connection is PostgreSQL.

    Migrations call this rather than the module-level constant so that
    multi-database setups (e.g. running migrations against a non-default
    connection) get the right answer.
    """
    return schema_editor.connection.vendor == "postgresql"
