"""Tests for the bitemporal mixin and its application to DNS models.

These tests only run on PostgreSQL. On MySQL CI runs they're skipped because
the bitemporal columns don't exist there (the migration is a no-op).
"""

import unittest

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.utils import timezone
from nautobot.apps.testing import TestCase
from nautobot.extras.models import Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix

from nautobot_dns_models.bitemporal import BITEMPORAL_ENABLED
from nautobot_dns_models.models import (
    ARecord,
    CNAMERecord,
    DNSRegistrar,
    DNSRegistration,
    DNSZone,
    TXTRecord,
)


def _make_zone(name, **overrides):
    """Create a DNSZone with all `full_clean`-required SOA fields populated.

    amend() runs full_clean() (per Hamilton C-1 fix) so any zone we plan to
    amend must satisfy the full validator suite. Tests that only use a zone
    as a container for other records can call DNSZone.objects.create directly.
    """
    defaults = {
        "filename": f"db.{name}",
        "soa_mname": f"ns.{name}",
        "soa_rname": f"hostmaster@{name}",
    }
    defaults.update(overrides)
    return DNSZone.objects.create(name=name, **defaults)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalSequencedAmendTests(TestCase):
    """A change to a tracked field on save() should close the prior row and insert a successor."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="example.com")
        cls.status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.0.0.0/24", namespace=namespace, type="Pool", status=cls.status)
        cls.ip1 = IPAddress.objects.create(address="10.0.0.1/32", namespace=namespace, status=cls.status)
        cls.ip2 = IPAddress.objects.create(address="10.0.0.2/32", namespace=namespace, status=cls.status)

    def test_first_save_opens_belief_window(self):
        """Fresh records get an entry_id and an open recorded_during,
        with valid_during.lower EXACTLY matching recorded_during.lower
        (the __init__ override snaps the per-field default skew together).
        """
        a = ARecord.objects.create(name="host.example.com", ip_address=self.ip1, zone=self.zone)
        a.refresh_from_db()
        self.assertIsNotNone(a.entry_id)
        self.assertIsNone(a.recorded_during.upper)
        self.assertIsNone(a.valid_during.upper)
        self.assertEqual(a.valid_during.lower, a.recorded_during.lower)

    def test_amend_closes_prior_and_creates_successor(self):
        """Explicit amend() rebinds self to a new pk with fresh entry_id."""
        a = ARecord.objects.create(name="host.example.com", ip_address=self.ip1, zone=self.zone)
        original_pk = a.pk
        original_entry_id = a.entry_id

        a.amend(description="moved here")

        # Self now points at the successor row.
        self.assertNotEqual(a.pk, original_pk)
        self.assertNotEqual(a.entry_id, original_entry_id)
        self.assertEqual(a.description, "moved here")

        # The prior row should still exist via all_versions, with a closed window.
        prior = ARecord.all_versions.get(pk=original_pk)
        self.assertIsNotNone(prior.recorded_during.upper)

        # ARecord.objects (current-only) sees exactly one row -- the successor.
        current = ARecord.objects.filter(name="host.example.com", zone=self.zone)
        self.assertEqual(current.count(), 1)
        self.assertEqual(current.first().pk, a.pk)

    def test_save_does_not_amend(self):
        """save() does in-place UPDATE; only amend() creates a new belief row.

        This is the deliberate API split: save() is framework-compatible
        (Nautobot UI / REST PATCH / view tests assume pk stability), while
        amend() is the explicit ``please rotate the belief log`` call.
        """
        a = ARecord.objects.create(name="static.example.com", ip_address=self.ip1, zone=self.zone)
        original_pk = a.pk
        a.description = "edited"
        a.save()
        self.assertEqual(a.pk, original_pk)
        self.assertEqual(
            ARecord.all_versions.filter(name="static.example.com", zone=self.zone).count(), 1
        )

    def test_history_contains_all_belief_rows(self):
        """history() returns every row that shares the natural key, oldest first."""
        a = ARecord.objects.create(name="multi.example.com", ip_address=self.ip1, zone=self.zone)
        a.amend(description="v2")
        a.amend(description="v3")

        hist = list(a.history())
        self.assertEqual(len(hist), 3)
        # Oldest first.
        for earlier, later in zip(hist, hist[1:]):
            self.assertLess(earlier.recorded_during.lower, later.recorded_during.lower)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalQuerySetTests(TestCase):
    """`.current()` and `.as_of(dt)` slice the belief log correctly."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="qs.example.com")
        cls.t0 = timezone.now()

    def test_current_excludes_amended_rows(self):
        z = _make_zone("zoneA.example")
        z.amend(description="v2")
        z.amend(description="v3")

        self.assertEqual(DNSZone.objects.filter(name="zoneA.example").count(), 1)
        self.assertEqual(DNSZone.all_versions.filter(name="zoneA.example").count(), 3)

    def test_as_of_returns_belief_at_instant(self):
        import time

        z = _make_zone("zoneB.example", description="v1")
        # Sleep a real (small) amount so the captured timestamp is strictly
        # after v1's open and strictly before v2's amend. Microsecond-level
        # gaps between Python statements aren't reliable across CI runners.
        time.sleep(0.01)
        between_v1_and_v2 = timezone.now()
        time.sleep(0.01)

        z.amend(description="v2")

        rows = DNSZone.all_versions.filter(name="zoneB.example").as_of(between_v1_and_v2)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().description, "v1")


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class CNAMEExclusivityHonorsCurrentBeliefsOnly(TestCase):
    """An amended-away CNAME must not block creation of an A record at the same name."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="exclusivity.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.0.1.0/24", namespace=namespace, type="Pool", status=status)
        cls.ip = IPAddress.objects.create(address="10.0.1.1/32", namespace=namespace, status=status)

    def test_amended_cname_does_not_block_new_arecord(self):
        # 1. Create a CNAME, then amend it (closing the prior belief).
        cname = CNAMERecord.objects.create(name="alias", alias="target.example", zone=self.zone)
        cname.amend(alias="target2.example")

        # 2. Now "remove" the CNAME by an amend that effectively retires it --
        # for this test we mark the row's recorded_during.upper to now via the
        # all_versions manager, simulating a deletion that closed the belief.
        from psycopg2.extras import DateTimeTZRange

        CNAMERecord.all_versions.filter(pk=cname.pk).update(
            recorded_during=DateTimeTZRange(
                lower=cname.recorded_during.lower, upper=timezone.now(), bounds="[)"
            )
        )

        # 3. Creating an A record at the same name should now succeed -- the
        # validator filters to current beliefs only.
        a = ARecord(name="alias", ip_address=self.ip, zone=self.zone)
        a.full_clean()  # would raise ValidationError pre-fix
        a.save()
        self.assertIsNotNone(a.pk)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class PartialUniqueIndexAllowsHistoricalRows(TestCase):
    """The partial unique index on (natural_key) WHERE upper(recorded_during) IS NULL
    must permit many closed-belief rows but still reject duplicate currents.
    """

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="unique.example")

    def test_multiple_historical_rows_allowed(self):
        t = TXTRecord.objects.create(name="spf", text="v=spf1 -all", zone=self.zone)
        for i in range(5):
            t.amend(text=f"v=spf1 v{i} -all")

        all_rows = TXTRecord.all_versions.filter(name="spf", zone=self.zone)
        self.assertEqual(all_rows.count(), 6)
        current = TXTRecord.objects.filter(name="spf", zone=self.zone)
        self.assertEqual(current.count(), 1)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalRegistrationTests(TestCase):
    """DNSRegistration bitemporal -- compliance trail across registrar field churn."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = _make_zone("registered.example")
        cls.registrar = DNSRegistrar.objects.create(name="TestRegistrar")
        cls.status = Status.objects.get(name="Active")
        # full_clean() validates that Status's content_types include the
        # model being saved. Pre-Hamilton C-1 fix, amend() bypassed
        # full_clean() so this wasn't required. Now it is.
        cls.status.content_types.add(ContentType.objects.get_for_model(DNSRegistration))

    def test_lock_field_change_creates_belief_row(self):
        reg = DNSRegistration.objects.create(
            dns_registrar=self.registrar,
            dns_zone=self.zone,
            status=self.status,
            transfer_locked=False,
        )
        reg.amend(transfer_locked=True)

        history = list(reg.history())
        self.assertEqual(len(history), 2)
        self.assertFalse(history[0].transfer_locked)
        self.assertTrue(history[1].transfer_locked)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalManagerSemanticsTests(TestCase):
    """Default manager (`objects`) is current-only; `all_versions` is unfiltered."""

    def test_default_manager_filters_to_current(self):
        z = _make_zone("manager.example")
        z.amend(description="v2")
        self.assertEqual(DNSZone.objects.filter(name="manager.example").count(), 1)
        self.assertEqual(DNSZone.all_versions.filter(name="manager.example").count(), 2)

    def test_all_versions_manager_is_present_on_every_bitemporal_model(self):
        """Every bitemporal model exposes `all_versions` for unfiltered access.

        Note: we intentionally do NOT set `Meta.base_manager_name` on the
        abstract mixin -- Django's abstract Meta inheritance doesn't propagate
        that option, and forcing it on every concrete model would override
        Nautobot's UI-natural behavior of showing current beliefs in
        reverse-FK contexts. Use `Model.all_versions` explicitly for the
        unfiltered view (which this test confirms is always wired up).
        """
        for model in [DNSZone, DNSRegistration, ARecord, CNAMERecord, TXTRecord]:
            self.assertTrue(
                hasattr(model, "all_versions"),
                msg=f"{model.__name__} should expose an all_versions manager",
            )
            # Sanity: all_versions is unfiltered (returns the same row count
            # as a plain `Model._base_manager.all()`).
            self.assertEqual(
                model.all_versions.all().count(),
                model._base_manager.all().count(),
            )


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalQuerySetIsRestricted(TestCase):
    """Regression: the BitemporalQuerySet must inherit Nautobot's RestrictedQuerySet
    so detail views and ObjectsTablePanel renders don't 500 on
    `queryset.restrict(user, action)`.
    """

    def test_restrict_method_is_available(self):
        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        user = user_model.objects.create(username="restricttest")
        DNSZone.objects.create(name="restrict.example")
        qs = DNSZone.objects.all()
        # If the queryset doesn't inherit RestrictedQuerySet this raises
        # AttributeError, which is exactly what the Phase K demo tour
        # surfaced as a 500 on every bitemporal detail view.
        restricted = qs.restrict(user, "view")
        self.assertIsNotNone(restricted)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalFilterSetAcceptsAsOf(TestCase):
    """Regression: the bitemporal FilterSets must accept `?as_of=<dt>` as a
    declared parameter so the strict-mode validation doesn't 400 before the
    viewset's BitemporalAPIMixin sees it.
    """

    def test_as_of_passes_filterset_validation(self):
        from nautobot_dns_models.filters import DNSZoneFilterSet

        # The filterset should declare `as_of` (via BitemporalFilterSetMixin)
        # so the param doesn't trip "Unknown filter field" in strict mode.
        fs = DNSZoneFilterSet(data={"as_of": "2026-05-27T17:00:00Z"})
        self.assertTrue(fs.is_valid(), msg=f"FilterSet rejected as_of: {fs.errors}")


class BitemporalDisabledOnMySQLTests(TestCase):
    """Sanity: on a non-Postgres backend, BITEMPORAL_ENABLED is False and the
    bitemporal fields aren't part of the model.

    This test runs on every backend so the assertion always lines up with
    reality. Postgres assertions go in the @skipUnless suites above.
    """

    def test_constant_matches_backend(self):
        expected = connection.vendor == "postgresql"
        self.assertEqual(BITEMPORAL_ENABLED, expected)

    def test_field_presence_matches_backend(self):
        field_names = {f.name for f in ARecord._meta.get_fields()}
        if BITEMPORAL_ENABLED:
            self.assertIn("valid_during", field_names)
            self.assertIn("recorded_during", field_names)
            self.assertIn("entry_id", field_names)
        else:
            self.assertNotIn("valid_during", field_names)
            self.assertNotIn("recorded_during", field_names)
            self.assertNotIn("entry_id", field_names)


# ============================================================================
# Failure-mode regression tests (Margaret Hamilton review, v2.2.0a2)
# ============================================================================


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class AmendValidationTests(TestCase):
    """C-1: amend() must call full_clean() so subclass validators run on the successor."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="validation.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.0.2.0/24", namespace=namespace, type="Pool", status=status)
        Prefix.objects.create(prefix="2001:db8:abcd:42::/64", namespace=namespace, type="Pool", status=status)
        cls.v4 = IPAddress.objects.create(address="10.0.2.1/32", namespace=namespace, status=status)
        cls.v6 = IPAddress.objects.create(address="2001:db8:abcd:42::1/128", namespace=namespace, status=status)

    def test_amend_arecord_with_v6_address_raises_validation_error(self):
        """ARecord.clean() rejects v6 addresses; amend() must invoke it."""
        from django.core.exceptions import ValidationError

        a = ARecord.objects.create(name="host.validation.example", ip_address=self.v4, zone=self.zone)
        with self.assertRaises(ValidationError):
            a.amend(ip_address=self.v6)

    def test_amend_validation_failure_leaves_prior_intact(self):
        """If amend() fails validation, the prior row's belief window must NOT have been closed."""
        from django.core.exceptions import ValidationError

        a = ARecord.objects.create(name="host2.validation.example", ip_address=self.v4, zone=self.zone)
        prior_pk = a.pk
        prior_recorded = a.recorded_during

        with self.assertRaises(ValidationError):
            a.amend(ip_address=self.v6)

        # The savepoint should have rolled back -- prior row's recorded_during
        # must still be open (upper is None).
        prior = ARecord.all_versions.get(pk=prior_pk)
        self.assertIsNone(prior.recorded_during.upper)
        self.assertEqual(prior.recorded_during.lower, prior_recorded.lower)
        # No successor row exists.
        self.assertEqual(
            ARecord.all_versions.filter(name="host2.validation.example", zone=self.zone).count(),
            1,
        )


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class AmendConcurrencyTests(TestCase):
    """C-2: amend() detects concurrent close of the prior row and raises ConcurrentAmendError."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="concurrency.example")

    def test_amend_on_already_closed_prior_raises_concurrent_amend_error(self):
        """Simulate the race: close prior out-of-band, then try to amend through it."""
        from psycopg2.extras import DateTimeTZRange

        from nautobot_dns_models.bitemporal import ConcurrentAmendError

        t = TXTRecord.objects.create(name="lock", text="v1", zone=self.zone)

        # Out-of-band: pretend another writer closed this row's recording window.
        TXTRecord.all_versions.filter(pk=t.pk).update(
            recorded_during=DateTimeTZRange(
                lower=t.recorded_during.lower, upper=timezone.now(), bounds="[)"
            )
        )

        # Now `t` in memory still thinks it's current, but the DB row is closed.
        # amend() must detect this and raise ConcurrentAmendError, not silently
        # produce a second close timestamp.
        with self.assertRaises(ConcurrentAmendError):
            t.amend(text="v2")

    def test_amend_on_deleted_prior_raises_concurrent_amend_error(self):
        """If the prior row was deleted between read and amend, raise rather than corrupt the chain."""
        from nautobot_dns_models.bitemporal import ConcurrentAmendError

        t = TXTRecord.objects.create(name="lock-deleted", text="v1", zone=self.zone)
        t_pk = t.pk

        # Out-of-band delete.
        TXTRecord.all_versions.filter(pk=t_pk).delete()

        with self.assertRaises(ConcurrentAmendError):
            t.amend(text="v2")


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class AmendInOuterTransactionTests(TestCase):
    """H-1: amend() inside a rolled-back outer transaction leaves self in a stale state."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="rollback.example")

    def test_outer_rollback_undoes_amend_but_leaves_self_stale(self):
        """The documented invariant: caller must re-fetch after outer rollback."""
        from django.db import transaction

        t = TXTRecord.objects.create(name="rollback", text="v1", zone=self.zone)
        original_pk = t.pk

        class RolledBack(Exception):
            pass

        try:
            with transaction.atomic():
                t.amend(text="v2")
                # self.pk has rotated to the successor's pk inside the txn...
                self.assertNotEqual(t.pk, original_pk)
                raise RolledBack()
        except RolledBack:
            pass

        # After the rollback, the *database* has only the original row;
        # the successor never committed.
        rows = TXTRecord.all_versions.filter(name="rollback", zone=self.zone)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().pk, original_pk)
        # `t.pk` is now stale -- it references a pk that doesn't exist.
        # This is the documented "caller must re-fetch" invariant.
        with self.assertRaises(TXTRecord.DoesNotExist):
            TXTRecord.all_versions.get(pk=t.pk)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class FirstSaveTimestampConsistencyTests(TestCase):
    """H-2: valid_during and recorded_during land at EXACTLY the same instant on first save."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="window-init.example")

    def test_valid_and_recorded_during_share_exact_lower_bound(self):
        """No microsecond skew -- they must be byte-identical on first INSERT."""
        z = DNSZone.objects.create(name="instant.example")
        z.refresh_from_db()
        # Exact equality -- not "within 1 second."
        self.assertEqual(z.valid_during.lower, z.recorded_during.lower)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class CNAMEExclusivityRaceTests(TestCase):
    """H-5: the advisory lock is acquired before the existence check.

    A true concurrency test requires multiple connections (Django TestCase
    wraps everything in a single transaction, so two `.objects.create()`
    calls from one thread can't actually race). This test instead asserts
    that the lock call is reachable -- if the implementation regresses
    and skips the lock, the test will fail.
    """

    @classmethod
    def setUpTestData(cls):
        from constance.test import override_config
        # Enable CNAME restriction for this suite
        cls._override = override_config(nautobot_dns_models__CNAME_RESTRICTION_ENABLED=True)
        cls._override.__enter__()
        cls.zone = DNSZone.objects.create(name="cname-race.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.0.3.0/24", namespace=namespace, type="Pool", status=status)
        cls.ip = IPAddress.objects.create(address="10.0.3.1/32", namespace=namespace, status=status)

    @classmethod
    def tearDownClass(cls):
        cls._override.__exit__(None, None, None)
        super().tearDownClass()

    def test_lock_call_is_reachable_during_cname_validation(self):
        """Sanity: creating a CNAME should issue the pg_advisory_xact_lock.

        We can't easily test concurrent races inside a single Django test
        transaction, so we verify the code path is taken by checking that
        the advisory lock is acquired (visible in pg_locks).
        """
        from constance.test import override_config

        with override_config(nautobot_dns_models__CNAME_RESTRICTION_ENABLED=True):
            CNAMERecord(name="race", alias="target.example", zone=self.zone).full_clean()

            # Inspect pg_locks to confirm the advisory lock was acquired in
            # this transaction. The lock is released at COMMIT, so it should
            # still be visible inside the test transaction.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
                )
                lock_count = cursor.fetchone()[0]
            # At least one advisory lock acquired during validation.
            self.assertGreaterEqual(lock_count, 1)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class HistoryEdgeCasesTests(TestCase):
    """L-3: history() handles missing natural_key_field_names gracefully."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="history-edge.example")

    def test_history_falls_back_to_pk_filter_when_no_natural_key(self):
        """If a subclass forgets natural_key_field_names, history() should still return SOMETHING (this row)."""
        # All real bitemporal models declare natural_key_field_names, so we
        # construct a mock by stripping the attribute temporarily.
        z = DNSZone.objects.create(name="no-nk.example")
        original = type(z).natural_key_field_names
        try:
            del type(z).natural_key_field_names
            rows = list(z.history())
            # Fallback: just this row, filtered by pk.
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].pk, z.pk)
        finally:
            type(z).natural_key_field_names = original
