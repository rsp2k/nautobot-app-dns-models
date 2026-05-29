"""
Bitemporal mixin for Nautobot DNS Models.

Adds two time axes to selected models:

- ``valid_during``    -- when the fact was true in the world.
- ``recorded_during`` -- when we (Nautobot) believed it.

Together they enable point-in-time replay ("what did we believe about zone X
on 2026-03-15?") and unambiguous reconciliation with late-arriving data from
registrars or external probes.

**Mutation contract**:

- ``obj.save()`` does a standard Django in-place UPDATE. The pk is stable.
- ``obj.amend(field=new_value)`` does the sequenced amend: close the prior
  ``recorded_during`` window, INSERT a successor row with a fresh
  ``entry_id``, and rebind ``self`` to the successor.

This split matters because Nautobot's UI views, REST endpoints, and the
testing framework all assume ``save()`` is in-place. Routing belief-log
mutations through an explicit ``amend()`` keeps both contracts intact.

PostgreSQL-only. The migration that physically adds the columns is a no-op on
non-Postgres backends; the mixin's runtime methods detect ``vendor != 'postgresql'``
and ``amend()`` falls back to a plain in-place UPDATE there.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from nautobot.core.models.managers import BaseManager
from nautobot.core.models.querysets import RestrictedQuerySet


class ConcurrentAmendError(Exception):
    """Raised when amend() detects another writer modified the prior row
    between the read and the close. Callers should re-read the instance
    via the manager and retry the amend on the fresh row.
    """


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


class BitemporalQuerySet(RestrictedQuerySet):
    """QuerySet with valid-time and recording-time query helpers.

    Inherits from Nautobot's ``RestrictedQuerySet`` (not plain
    ``models.QuerySet``) so ``.restrict(user, "view")`` works -- that's the
    method Nautobot's ``ObjectsTablePanel``, ``NautobotUIViewSet``, and the
    row-level permission system call on every nested queryset. Forgetting
    this inheritance 500s every detail page that touches a bitemporal model.
    """

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


class BitemporalManager(BaseManager.from_queryset(BitemporalQuerySet)):
    """Default manager that filters to the *current* belief row.

    Inherits Nautobot's ``BaseManager`` so ``get_by_natural_key()`` works
    (Nautobot's serializer framework and ``test_natural_key_symmetry`` both
    depend on it being available on every model manager).

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


class AllVersionsManager(BaseManager.from_queryset(BitemporalQuerySet)):
    """Non-default manager that returns every belief row including amended-away ones."""


class BitemporalMixin(models.Model):
    """
    Abstract mixin that gives a model two time axes plus an explicit
    ``amend()`` method for sequenced amends.

    Concrete models inheriting this gain three columns (on Postgres):

    - ``valid_during``: when the fact was/is true in the world
    - ``recorded_during``: when this row was the current belief
    - ``entry_id``: distinguishes successive belief rows about the same
      natural key

    ``save()`` is a plain Django save -- in-place UPDATE on existing rows,
    pk stable. Use :meth:`amend` to create a new belief row reflecting a
    real-world change. See the module docstring for the mutation contract.
    """

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

    def _snap_window_skew_if_auto_defaulted(self) -> None:
        """H-2: snap valid_during.lower to recorded_during.lower on first INSERT.

        Django invokes `default=_open_belief_window` PER-FIELD, so the two
        windows land microseconds apart on a fresh instance. The skew breaks
        `as_of(t)` queries where t lands between the two timestamps. If both
        windows are open and their lower bounds are within ~1ms (i.e., they
        came from the auto-applied default callable rather than an explicit
        backdate), snap them to share the recorded timestamp.
        """
        if not BITEMPORAL_ENABLED:
            return
        valid = getattr(self, "valid_during", None)
        recorded = getattr(self, "recorded_during", None)
        if valid is None or recorded is None:
            return
        try:
            delta = abs((valid.lower - recorded.lower).total_seconds())
        except (AttributeError, TypeError):
            return
        if (
            valid.upper is None
            and recorded.upper is None
            and delta < 0.001  # 1 ms tolerance for "both auto-applied just now"
        ):
            self.valid_during = recorded

    objects = BitemporalManager()
    all_versions = AllVersionsManager()

    class Meta:
        abstract = True
        # NOTE: `base_manager_name` is intentionally NOT set. Django's abstract
        # Meta inheritance doesn't propagate this option to concrete subclasses,
        # so setting it here would be a no-op. The deliberate consequence:
        # reverse-FK traversal (e.g. `zone.arecord_set.all()`) uses the default
        # manager, which returns CURRENT beliefs only -- matching Nautobot UI
        # expectations. Callers needing the full belief log should reach for
        # `Model.all_versions` explicitly.

    # --------------------------------------------------------------------- save

    def save(self, *args, **kwargs):
        """Standard Django save. Initializes bitemporal columns on first INSERT.

        IMPORTANT: ``save()`` does NOT create a new belief row on update --
        that's an in-place UPDATE, matching Django/Nautobot framework
        expectations (the UI's edit-view test, REST PATCH, and PUT
        round-trip all assume pk stability across edits).

        To create a new belief row reflecting a real-world change, call
        :meth:`amend` explicitly. The two methods have distinct semantics:

        - ``obj.save()``                    -> UPDATE existing row in place
        - ``obj.amend(field=new_value)``    -> close prior, INSERT successor

        On non-Postgres backends, ``amend()`` falls back to a plain in-place
        UPDATE (no belief log) and behaves identically to ``save()``.
        """
        if self._state.adding:
            self._initialize_bitemporal_fields_if_needed()
            self._snap_window_skew_if_auto_defaulted()
        return super().save(*args, **kwargs)

    def amend(self, **field_changes):
        """Create a new belief row reflecting ``field_changes``.

        On Postgres:
            1. Acquire a row-level lock on the prior row (``SELECT FOR UPDATE``).
            2. Verify the prior row is still the current belief (raise
               :class:`ConcurrentAmendError` if another writer already
               closed it).
            3. Close the prior row's ``recorded_during`` window via raw
               ``UPDATE`` (bypassing ``save()`` to avoid ticking
               ``last_updated``).
            4. Run ``full_clean()`` on the in-memory instance with the
               amended values applied -- subclass validators (ARecord
               IPv4 check, CNAME exclusivity, total wire-length) run on
               the successor before INSERT.
            5. Insert a successor row carrying ``field_changes`` overlaid
               on the prior row's values, with a fresh ``entry_id`` and an
               open ``recorded_during`` window. ``valid_during`` carries
               over by default (the fact's wall-clock truth window hasn't
               changed, only our belief about it) -- pass
               ``valid_during=...`` to override.
            6. Mutate ``self`` to point at the successor so callers can
               keep using the same Python instance after the amend.

        On MySQL or other non-Postgres backends: applies ``field_changes``
        via attribute assignment and calls ``save()``. No belief log,
        same behavior as a plain edit.

        Example::

            arecord, created = ARecord.objects.get_or_create(...)
            if not created and wire_data_differs(arecord, scan):
                arecord.amend(_ttl=scan.ttl, description=scan.desc)

        Caller invariants:
            - **Outer-transaction rollback**: ``amend()`` opens a savepoint
              via ``@transaction.atomic``. If the *caller's* outer
              transaction is later rolled back, the bitemporal mutations
              are reversed -- but the in-memory ``self`` already had its
              ``pk`` and ``entry_id`` rotated. After such a rollback,
              ``self`` references a row that doesn't exist in the
              database; the next ``self.save()`` or ``self.refresh_from_db()``
              will raise ``DoesNotExist``. Callers in an outer transaction
              should either (a) catch the rollback and re-fetch the
              instance via the manager, or (b) treat the returned ``self``
              as opaque until the outer transaction commits.

            - **Retries**: catch :class:`ConcurrentAmendError`, re-fetch
              the instance via ``Model.objects.get(...)`` to see the
              successor that the other writer created, then retry amend
              on the fresh row. Do NOT retry on the same in-memory
              instance -- its ``recorded_during`` is stale.

            - **Validation**: subclass ``clean()`` overrides are invoked.
              Any ``ValidationError`` raised inside ``full_clean()``
              rolls back the savepoint cleanly (prior row's close is
              undone, no successor is inserted).

        Raises:
            ValueError: if called on an unsaved instance (no prior to close).
            ConcurrentAmendError: if another writer already closed the
                prior row, or if the prior row vanished between read and
                update.
            ValidationError: if ``full_clean()`` rejects the successor.
        """
        if self._state.adding or self.pk is None:
            raise ValueError(
                "amend() requires an existing row; call save() to create the first belief."
            )
        if not BITEMPORAL_ENABLED:
            # MySQL / other: plain in-place update.
            for field, value in field_changes.items():
                setattr(self, field, value)
            self.save()
            return self

        for field, value in field_changes.items():
            setattr(self, field, value)
        self._sequenced_amend()
        return self

    def _initialize_bitemporal_fields_if_needed(self) -> None:
        """Initialize recorded_during, valid_during, and entry_id on first INSERT.

        Captures `timezone.now()` ONCE and assigns the same DateTimeTZRange
        instance to both windows. This guarantees `valid_during.lower ==
        recorded_during.lower` on creation -- relying on a per-field
        `default=_open_belief_window` callable would invoke `now()` twice
        and produce microsecond skew, breaking `as_of(t)` queries for `t`
        landing between the two timestamps.
        """
        if not BITEMPORAL_ENABLED:
            return
        if getattr(self, "recorded_during", None) is None:
            shared_window = _open_belief_window()
            self.recorded_during = shared_window
            if getattr(self, "valid_during", None) is None:
                # By default, valid time tracks recording time on initial insert.
                # Ingest pipelines that backdate facts should set valid_during
                # explicitly before save() -- in that case we'll skip this branch.
                self.valid_during = shared_window
        elif getattr(self, "valid_during", None) is None:
            # Caller set recorded_during explicitly but left valid_during empty.
            self.valid_during = self.recorded_during
        if not getattr(self, "entry_id", None):
            self.entry_id = uuid.uuid4()

    @transaction.atomic
    def _sequenced_amend(self) -> None:
        """Close the prior belief row, insert a successor, rebind ``self``.

        Called by :meth:`amend` after the caller has applied field updates to
        the in-memory instance. The successor row inherits whatever
        ``valid_during`` is currently on ``self`` (so an explicit override
        in the amend call survives; otherwise the prior row's window
        carries over).

        Concurrency guarantee:
            Acquires a row-level lock on the prior row via
            ``SELECT ... FOR UPDATE`` inside this atomic block. Concurrent
            ``amend()`` calls on the same prior row serialize at the lock,
            and the second arrival sees the prior already-closed and raises
            :class:`ConcurrentAmendError` rather than silently corrupting
            the close timestamp.

        Validation guarantee:
            Calls ``full_clean()`` on the successor before INSERT so
            subclass-level validators (``ARecord``'s IPv4 check,
            CNAME-exclusivity, total wire-length) run on the new belief
            row, not just on the first ``save()`` of an instance.
        """
        type_ = type(self)
        prior_pk = self.pk

        # 1. Lock the prior row inside this savepoint. Concurrent amend() waits
        # here until the other writer commits or rolls back. After the lock
        # is granted, re-read the prior's recorded_during from the locked row
        # (NOT from `self`) -- the locked row is authoritative for whether
        # someone else closed it while we were waiting.
        try:
            prior = type_.all_versions.select_for_update().get(pk=prior_pk)
        except type_.DoesNotExist:
            raise ConcurrentAmendError(
                f"Prior belief row {prior_pk} was deleted between read and amend. "
                "Re-read the instance and retry."
            )

        if prior.recorded_during is not None and prior.recorded_during.upper is not None:
            raise ConcurrentAmendError(
                f"Belief row {prior_pk} was already closed by another writer "
                f"at {prior.recorded_during.upper}. Re-read the instance via "
                f"{type_.__name__}.objects and retry amend on the fresh row."
            )

        prior_recorded_during = prior.recorded_during
        now = timezone.now()

        # 2. Close the prior row's recording window. Use queryset.update() to
        # bypass save() and avoid ticking last_updated on the historical row.
        # Assert exactly one row was affected -- a 0-row result means the
        # prior vanished between SELECT FOR UPDATE and UPDATE (shouldn't
        # happen with the lock held, but defensive checks are cheap).
        affected = type_.all_versions.filter(pk=prior_pk).update(
            recorded_during=DateTimeTZRange(
                lower=prior_recorded_during.lower if prior_recorded_during else None,
                upper=now,
                bounds="[)",
            )
        )
        if affected != 1:
            raise ConcurrentAmendError(
                f"Expected to close exactly 1 prior belief row (pk={prior_pk}); "
                f"the UPDATE affected {affected} rows. The audit chain is at risk; "
                f"abort to preserve invariants."
            )

        # 3. Insert the successor as a fresh row carrying the in-memory values.
        # Run full_clean() FIRST -- subclass validators (ARecord IPv4 check,
        # CNAME exclusivity, total wire-length) must run on the successor
        # values, not just on first save(). Without this, amend() can write
        # invalid rows silently.
        self.pk = None
        self.id = None  # for UUID PKs Django assigns a new one on save()
        self.entry_id = uuid.uuid4()
        self.recorded_during = DateTimeTZRange(lower=now, upper=None, bounds="[)")
        self._state.adding = True
        self.full_clean()
        super().save()

    # -------------------------------------------------------- history accessors

    def history(self) -> models.QuerySet:
        """Every belief row that shares this row's natural-key identity.

        Default implementation matches on the model-declared natural-key
        fields (``natural_key_field_names``). Subclasses may override for
        more complex natural keys.
        """
        natural_key = getattr(type(self), "natural_key_field_names", None)
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
