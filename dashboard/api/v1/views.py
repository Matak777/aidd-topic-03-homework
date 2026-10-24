from django_filters import rest_framework as filters
from rest_framework import permissions, viewsets
from rest_framework.filters import OrderingFilter, SearchFilter

from dashboard.models import Share

from .serializer import ShareSerializer


class ShareFilter(filters.FilterSet):
    class Meta:
        model = Share
        fields = ("name", "ticker")


class ShareViewSet(viewsets.ModelViewSet):
    serializer_class = ShareSerializer
    queryset = Share.objects.all()
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = (OrderingFilter, filters.DjangoFilterBackend, SearchFilter)
    filterset_class = ShareFilter
    ordering_fields = ["name", "ticker"]
    search_fields = ["$name"]
