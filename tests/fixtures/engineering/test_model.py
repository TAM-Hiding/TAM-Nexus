import unittest

from model import micrometres_from_millimetres


class EngineeringUnits(unittest.TestCase):
    def test_fixture_conversion(self):
        self.assertEqual(micrometres_from_millimetres(2), 2000)
