"""Filtering for nautobot_dns_models."""

import django_filters
from django.db.models import F
from django.db.models.functions import Coalesce
from nautobot.apps.filters import NautobotFilterSet, SearchFilter, TenancyModelFilterSetMixin
from nautobot.core.filters import MultiValueCharFilter, NaturalKeyOrPKMultipleChoiceFilter
from netaddr import IPAddress as NetIPAddress

from nautobot_dns_models import models
from nautobot_dns_models.bitemporal import BITEMPORAL_ENABLED

EXPIRATION_DATE_INPUT_FORMATS = ("%Y-%m-%d",)


# django-filter cannot auto-generate a Filter for DateTimeRangeField -- it
# raises AssertionError at FilterSet class-definition time if `fields="__all__"`
# tries to include `valid_during` / `recorded_during`. We exclude them here so
# autogen succeeds; point-in-time querying is provided by the viewset-level
# `?as_of=<iso8601>` param in `BitemporalAPIMixin` (api/views.py), which runs
# before the filterset processes any params. On MySQL the bitemporal fields
# don't exist on the model, so this tuple resolves empty and the `exclude`
# entry is harmless.
BITEMPORAL_FILTERSET_EXCLUDE = (
    ("valid_during", "recorded_during", "entry_id") if BITEMPORAL_ENABLED else ()
)


class BitemporalFilterSetMixin(django_filters.FilterSet):
    """Declare `?as_of=<iso8601>` as a no-op filter so strict-mode validation
    doesn't reject it.

    The actual as_of belief-window switch happens in
    ``BitemporalAPIMixin.get_queryset()`` (api/views.py) -- but Nautobot
    enables django-filter's strict mode globally, so the filterset
    validates query params first and rejects any param it doesn't
    declare with HTTP 400. Declaring `as_of` here as a method-only
    filter that returns the queryset unchanged lets the param pass
    validation; the viewset's mixin then consumes it for real.

    NOTE: Must inherit from ``django_filters.FilterSet`` (not a plain
    ``object`` mixin) so the FilterSetMetaclass picks up the declared
    ``as_of`` filter -- the metaclass only walks bases that have a
    ``declared_filters`` attribute, which only ``FilterSet`` subclasses do.
    """

    as_of = django_filters.IsoDateTimeFilter(method="_noop_as_of")

    def _noop_as_of(self, queryset, name, value):  # pylint: disable=unused-argument
        # Handled at the viewset layer in BitemporalAPIMixin.get_queryset().
        return queryset


class DNSViewFilterSet(NautobotFilterSet):
    """Filter for DNSView."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.DNSView
        fields = "__all__"


class DNSViewPrefixAssignmentFilterSet(NautobotFilterSet):
    """Filter for DNSViewPrefixAssignment."""

    q = SearchFilter(
        filter_predicates={
            "dns_view__name": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.DNSViewPrefixAssignment
        fields = "__all__"


class DNSRegistrarFilterSet(NautobotFilterSet):
    """Filter for DNSRegistrar."""

    url = MultiValueCharFilter(lookup_expr="icontains")
    account_number = MultiValueCharFilter(lookup_expr="icontains")

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "url": "icontains",
            "account_number": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.DNSRegistrar
        fields = "__all__"


class DNSRegistrationFilterSet(BitemporalFilterSetMixin, NautobotFilterSet):
    """Filter for DNSRegistration."""

    expiration_date__lte = django_filters.DateFilter(
        field_name="expiration_date",
        lookup_expr="lte",
        input_formats=EXPIRATION_DATE_INPUT_FORMATS,
    )
    expiration_date__gte = django_filters.DateFilter(
        field_name="expiration_date",
        lookup_expr="gte",
        input_formats=EXPIRATION_DATE_INPUT_FORMATS,
    )

    q = SearchFilter(
        filter_predicates={
            "dns_registrar__name": "icontains",
            "dns_zone__name": "icontains",
            "status__name": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.DNSRegistration
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class DNSZoneFilterSet(BitemporalFilterSetMixin, TenancyModelFilterSetMixin, NautobotFilterSet):
    """Filter for DNSZone."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "filename": "icontains",
            "soa_mname": "icontains",
            "soa_rname": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.DNSZone
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


# pylint: disable=nb-no-model-found, nb-warn-dunder-filter-field
class DNSRecordFilterSet(BitemporalFilterSetMixin, NautobotFilterSet):
    """Base filter for all DNSRecord models, with support for effective TTL.

    Mixing in ``BitemporalFilterSetMixin`` here propagates the ``as_of``
    declared filter to every concrete record FilterSet via MRO inheritance.
    """

    zone = NaturalKeyOrPKMultipleChoiceFilter(
        queryset=models.DNSZone.objects.all(),
        to_field_name="name",
        label="Zone (name or ID)",
    )

    ttl = django_filters.NumberFilter(method="filter_ttl", label="TTL")
    ttl__ne = django_filters.NumberFilter(method="filter_ttl_ne")
    ttl__gte = django_filters.NumberFilter(method="filter_ttl", lookup_expr="gte")
    ttl__lte = django_filters.NumberFilter(method="filter_ttl", lookup_expr="lte")
    ttl__gt = django_filters.NumberFilter(method="filter_ttl", lookup_expr="gt")
    ttl__lt = django_filters.NumberFilter(method="filter_ttl", lookup_expr="lt")

    def filter_ttl(self, queryset, name, value):
        """Filter by effective TTL (use record's TTL if set, otherwise zone's TTL)."""
        queryset = queryset.annotate(effective_ttl=Coalesce(F("_ttl"), F("zone__ttl")))
        lookup = name.split("__")[-1] if "__" in name else "exact"
        return queryset.filter(**{f"effective_ttl__{lookup}": value})

    def filter_ttl_ne(self, queryset, name, value):  # pylint: disable=unused-argument
        """Exclude effective TTL equal to value."""
        queryset = queryset.annotate(effective_ttl=Coalesce(F("_ttl"), F("zone__ttl")))
        return queryset.exclude(effective_ttl=value)


class NSRecordFilterSet(DNSRecordFilterSet):
    """Filter for NSRecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "server": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.NSRecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


def ip_address_preprocessor(value):
    """Validate IP address input."""
    try:
        NetIPAddress(value)
    except Exception as error:
        raise ValueError("Invalid IP address") from error
    return value


class ARecordFilterSet(DNSRecordFilterSet):
    """Filter for ARecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "ip_address__host": {"lookup_expr": "net_host", "preprocessor": ip_address_preprocessor},
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.ARecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class AAAARecordFilterSet(DNSRecordFilterSet):
    """Filter for AAAARecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "ip_address__host": {"lookup_expr": "net_host", "preprocessor": ip_address_preprocessor},
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.AAAARecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class CNAMERecordFilterSet(DNSRecordFilterSet):
    """Filter for CNAMERecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "alias": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.CNAMERecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class MXRecordFilterSet(DNSRecordFilterSet):
    """Filter for MXRecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "mail_server": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.MXRecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class TXTRecordFilterSet(DNSRecordFilterSet):
    """Filter for TXTRecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "text": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.TXTRecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class PTRRecordFilterSet(DNSRecordFilterSet):
    """Filter for PTRRecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "ptrdname": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.PTRRecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE


class SRVRecordFilterSet(DNSRecordFilterSet):
    """Filter for SRVRecord."""

    q = SearchFilter(
        filter_predicates={
            "name": "icontains",
            "zone__name": "icontains",
            "target": "icontains",
        }
    )

    class Meta:
        """Meta attributes for filter."""

        model = models.SRVRecord
        fields = "__all__"
        exclude = BITEMPORAL_FILTERSET_EXCLUDE
