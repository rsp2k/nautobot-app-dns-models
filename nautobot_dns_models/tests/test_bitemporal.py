"""Tests for the bitemporal mixin and its application to DNS models.

These tests only run on PostgreSQL. On MySQL CI runs they're skipped because
the bitemporal columns don't exist there (the migration is a no-op).
"""

import unittest

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
        """Fresh records get an entry_id and an open recorded_during."""
        a = ARecord.objects.create(name="host.example.com", ip_address=self.ip1, zone=self.zone)
        a.refresh_from_db()
        self.assertIsNotNone(a.entry_id)
        self.assertIsNone(a.recorded_during.upper)
        # valid_during defaults to mirror recorded_during on first save.
        self.assertEqual(a.valid_during.lower, a.recorded_during.lower)

    def test_amend_closes_prior_and_creates_successor(self):
        """Changing a tracked field rebinds self to a new pk with fresh entry_id."""
        a = ARecord.objects.create(name="host.example.com", ip_address=self.ip1, zone=self.zone)
        original_pk = a.pk
        original_entry_id = a.entry_id

        a.description = "moved here"
        a.save()

        # Self now points at the successor row.
        self.assertNotEqual(a.pk, original_pk)
        self.assertNotEqual(a.entry_id, original_entry_id)

        # The prior row should still exist via all_versions, with a closed window.
        prior = ARecord.all_versions.get(pk=original_pk)
        self.assertIsNotNone(prior.recorded_during.upper)

        # ARecord.objects (current-only) sees exactly one row -- the successor.
        current = ARecord.objects.filter(name="host.example.com", zone=self.zone)
        self.assertEqual(current.count(), 1)
        self.assertEqual(current.first().pk, a.pk)

    def test_non_tracked_field_change_does_not_amend(self):
        """Editing only last_updated / _custom_field_data should not produce a new row."""
        a = ARecord.objects.create(name="static.example.com", ip_address=self.ip1, zone=self.zone)
        original_pk = a.pk
        a._custom_field_data = {"note": "edited"}
        a.save()
        self.assertEqual(a.pk, original_pk)
        # Only one belief row.
        self.assertEqual(
            ARecord.all_versions.filter(name="static.example.com", zone=self.zone).count(), 1
        )

    def test_history_contains_all_belief_rows(self):
        """history() returns every row that shares the natural key, oldest first."""
        a = ARecord.objects.create(name="multi.example.com", ip_address=self.ip1, zone=self.zone)
        a.description = "v2"
        a.save()
        a.description = "v3"
        a.save()

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
        z = DNSZone.objects.create(name="zoneA.example")
        z.description = "v2"
        z.save()
        z.description = "v3"
        z.save()

        self.assertEqual(DNSZone.objects.filter(name="zoneA.example").count(), 1)
        self.assertEqual(DNSZone.all_versions.filter(name="zoneA.example").count(), 3)

    def test_as_of_returns_belief_at_instant(self):
        import time

        z = DNSZone.objects.create(name="zoneB.example", description="v1")
        # Sleep a real (small) amount so the captured timestamp is strictly
        # after v1's open and strictly before v2's amend. Microsecond-level
        # gaps between Python statements aren't reliable across CI runners.
        time.sleep(0.01)
        between_v1_and_v2 = timezone.now()
        time.sleep(0.01)

        z.description = "v2"
        z.save()

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
        cname.alias = "target2.example"
        cname.save()

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
            t.text = f"v=spf1 v{i} -all"
            t.save()

        all_rows = TXTRecord.all_versions.filter(name="spf", zone=self.zone)
        self.assertEqual(all_rows.count(), 6)
        current = TXTRecord.objects.filter(name="spf", zone=self.zone)
        self.assertEqual(current.count(), 1)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalRegistrationTests(TestCase):
    """DNSRegistration bitemporal -- compliance trail across registrar field churn."""

    @classmethod
    def setUpTestData(cls):
        cls.zone = DNSZone.objects.create(name="registered.example")
        cls.registrar = DNSRegistrar.objects.create(name="TestRegistrar")
        cls.status = Status.objects.get(name="Active")

    def test_lock_field_change_creates_belief_row(self):
        reg = DNSRegistration.objects.create(
            dns_registrar=self.registrar,
            dns_zone=self.zone,
            status=self.status,
            transfer_locked=False,
        )
        reg.transfer_locked = True
        reg.save()

        history = list(reg.history())
        self.assertEqual(len(history), 2)
        self.assertFalse(history[0].transfer_locked)
        self.assertTrue(history[1].transfer_locked)


@unittest.skipUnless(BITEMPORAL_ENABLED, "Bitemporal features require PostgreSQL")
class BitemporalManagerSemanticsTests(TestCase):
    """Default manager (`objects`) is current-only; `all_versions` is unfiltered."""

    def test_default_manager_filters_to_current(self):
        z = DNSZone.objects.create(name="manager.example")
        z.description = "v2"
        z.save()
        self.assertEqual(DNSZone.objects.filter(name="manager.example").count(), 1)
        self.assertEqual(DNSZone.all_versions.filter(name="manager.example").count(), 2)

    def test_base_manager_is_all_versions(self):
        """Reverse FK traversal should use all_versions so amended rows are still reachable."""
        from nautobot_dns_models.models import DNSRecord  # for the base_manager_name check

        # The Meta.base_manager_name on the mixin is set to "all_versions" so that
        # `zone.arecord_set.all()` returns every historical A record. Verify the
        # value is propagated to concrete subclasses.
        for model in [DNSZone, DNSRegistration, ARecord, CNAMERecord, TXTRecord]:
            self.assertEqual(
                model._meta.base_manager_name,
                "all_versions",
                msg=f"{model.__name__} should use all_versions as base manager",
            )


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
