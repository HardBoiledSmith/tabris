from django.conf.urls import url
from rest_framework import serializers
from rest_framework import viewsets
from rest_framework.response import Response


def get_urlpatterns():
    as_view = ViewSet.as_view({
        'get': 'retrieve',
    })

    return [
        url(r'^numbers/$', as_view),
    ]


class ViewSet(viewsets.ViewSet):
    permission_classes = []
    serializer_class = serializers.Serializer
    throttle_classes = ()

    @staticmethod
    def retrieve(_):
        return Response()
