from factory import fuzzy
from rest_framework import status
from rest_framework.test import APITestCase


class APITest(APITestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_00(self):
        url = '/numbers/'
        data = dict()
        data['start'] = fuzzy.FuzzyInteger(100)
        data['end'] = fuzzy.FuzzyInteger(100)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
