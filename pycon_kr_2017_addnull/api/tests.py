from factory import fuzzy
from rest_framework import status
from rest_framework.test import APITestCase


class APITest(APITestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_00(self):
        path = '/numbers/'
        data = dict()
        data['start'] = fuzzy.FuzzyInteger(100).fuzz()
        data['end'] = fuzzy.FuzzyInteger(100).fuzz()

        response = self.client.get(path, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
