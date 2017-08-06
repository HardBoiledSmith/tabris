from django.conf.urls import url
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response


def get_urlpatterns():
    as_view = ViewSet.as_view({
        'get': 'list',
    })

    return [
        url(r'^numbers/$', as_view),
    ]


class Serializer(serializers.Serializer):
    start = serializers.IntegerField(required=True)
    end = serializers.IntegerField(required=True)

    def create(self, validated_data):
        pass

    def update(self, instance, validated_data):
        pass


class ViewSet(viewsets.ViewSet):
    @staticmethod
    def list(request):
        serializer = Serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        start = serializer.data['start']
        end = serializer.data['end']

        if start > end:
            data = dict()
            data['message'] = '\'start\' cannot be greater than \'end\'.'
            response = Response(status=status.HTTP_400_BAD_REQUEST, data=data)
            return response

        data = dict()
        data['contents'] = list(range(start, end))

        response = Response(data=data)

        return response
