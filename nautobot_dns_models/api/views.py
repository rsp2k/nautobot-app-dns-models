"""API views for nautobot_dns_models."""

from django.utils.dateparse import parse_datetime
from nautobot.apps.api import NautobotModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from nautobot_dns_models.bitemporal import BITEMPORAL_ENABLED
from nautobot_dns_models.api.serializers import (
    AAAARecordSerializer,
    ARecordSerializer,
    CNAMERecordSerializer,
    DNSRegistrarSerializer,
    DNSRegistrationSerializer,
    DNSViewPrefixAssignmentSerializer,
    DNSViewSerializer,
    DNSZoneSerializer,
    MXRecordSerializer,
    NSRecordSerializer,
    PTRRecordSerializer,
    SRVRecordSerializer,
    TXTRecordSerializer,
)
from nautobot_dns_models.filters import (
    AAAARecordFilterSet,
    ARecordFilterSet,
    CNAMERecordFilterSet,
    DNSRegistrarFilterSet,
    DNSRegistrationFilterSet,
    DNSViewFilterSet,
    DNSViewPrefixAssignmentFilterSet,
    DNSZoneFilterSet,
    MXRecordFilterSet,
    NSRecordFilterSet,
    PTRRecordFilterSet,
    SRVRecordFilterSet,
    TXTRecordFilterSet,
)
from nautobot_dns_models.models import (
    AAAARecord,
    ARecord,
    CNAMERecord,
    DNSRegistrar,
    DNSRegistration,
    DNSView,
    DNSViewPrefixAssignment,
    DNSZone,
    MXRecord,
    NSRecord,
    PTRRecord,
    SRVRecord,
    TXTRecord,
)


class BitemporalAPIMixin:
    """
    REST extensions for bitemporal models.

    - ``?as_of=<iso8601>`` on list endpoints returns the belief state
      Nautobot held at the given instant.
    - ``GET <detail>/history/`` returns every belief row sharing this row's
      natural key, oldest first.

    Both extensions are no-ops on MySQL (BITEMPORAL_ENABLED is False).
    """

    def get_queryset(self):
        qs = super().get_queryset()
        if not BITEMPORAL_ENABLED:
            return qs
        as_of = self.request.query_params.get("as_of") if hasattr(self, "request") else None
        if not as_of:
            return qs
        parsed = parse_datetime(as_of)
        if parsed is None:
            raise ParseError(detail="`as_of` must be an ISO 8601 datetime (e.g. 2026-05-27T12:00:00Z).")
        # Switch to the all-versions manager since we're reaching beyond
        # the current belief slice. Filter on recorded_during__contains.
        model = qs.model
        return model.all_versions.filter(recorded_during__contains=parsed)

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, *args, **kwargs):
        """Return every belief row that shares this row's natural key, oldest first."""
        if not BITEMPORAL_ENABLED:
            return Response(
                {"detail": "Bitemporal history is only available on PostgreSQL."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        obj = self.get_object()
        rows = obj.history()
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)


class DNSViewViewSet(NautobotModelViewSet):
    """DNSView API ViewSet."""

    queryset = DNSView.objects.all()
    serializer_class = DNSViewSerializer
    filterset_class = DNSViewFilterSet

    lookup_field = "pk"
    # Option for modifying the default HTTP methods:
    # http_method_names = ["get", "post", "put", "patch", "delete", "head", "options", "trace"]


class DNSViewPrefixAssignmentViewSet(NautobotModelViewSet):
    """DNSViewPrefixAssignment API ViewSet."""

    queryset = DNSViewPrefixAssignment.objects.all()
    serializer_class = DNSViewPrefixAssignmentSerializer
    filterset_class = DNSViewPrefixAssignmentFilterSet


class DNSRegistrarViewSet(NautobotModelViewSet):
    """DNSRegistrar API ViewSet."""

    queryset = DNSRegistrar.objects.all()
    serializer_class = DNSRegistrarSerializer
    filterset_class = DNSRegistrarFilterSet


class DNSRegistrationViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """DNSRegistration API ViewSet."""

    queryset = DNSRegistration.objects.all()
    serializer_class = DNSRegistrationSerializer
    filterset_class = DNSRegistrationFilterSet


class DNSZoneViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """DNSZone API ViewSet."""

    queryset = DNSZone.objects.all()
    serializer_class = DNSZoneSerializer
    filterset_class = DNSZoneFilterSet

    lookup_field = "pk"


class NSRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """NSRecord API ViewSet."""

    queryset = NSRecord.objects.all()
    serializer_class = NSRecordSerializer
    filterset_class = NSRecordFilterSet

    lookup_field = "pk"


class ARecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """ARecord API ViewSet."""

    queryset = ARecord.objects.all()
    serializer_class = ARecordSerializer
    filterset_class = ARecordFilterSet

    lookup_field = "pk"


class AAAARecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """AAAARecord API ViewSet."""

    queryset = AAAARecord.objects.all()
    serializer_class = AAAARecordSerializer
    filterset_class = AAAARecordFilterSet

    lookup_field = "pk"


class CNameRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """CNameRecord API ViewSet."""

    queryset = CNAMERecord.objects.all()
    serializer_class = CNAMERecordSerializer
    filterset_class = CNAMERecordFilterSet

    lookup_field = "pk"


class MXRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """MXRecord API ViewSet."""

    queryset = MXRecord.objects.all()
    serializer_class = MXRecordSerializer
    filterset_class = MXRecordFilterSet

    lookup_field = "pk"


class TXTRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """TXTRecord API ViewSet."""

    queryset = TXTRecord.objects.all()
    serializer_class = TXTRecordSerializer
    filterset_class = TXTRecordFilterSet

    lookup_field = "pk"


class PTRRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """PTRRecord API ViewSet."""

    queryset = PTRRecord.objects.all()
    serializer_class = PTRRecordSerializer
    filterset_class = PTRRecordFilterSet

    lookup_field = "pk"


class SRVRecordViewSet(BitemporalAPIMixin, NautobotModelViewSet):
    """SRVRecord API ViewSet."""

    queryset = SRVRecord.objects.all()
    serializer_class = SRVRecordSerializer
    filterset_class = SRVRecordFilterSet

    lookup_field = "pk"
