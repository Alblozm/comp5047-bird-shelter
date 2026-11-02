"""Protocol/safety tests use fake serial only; no connected actuator is driven."""
import io
import json
import math
from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hardware_test as hw

class BenchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.bench = hw.Bench()
        self.link = hw.Link(self.bench, 'climate')
        self.bench.connection['climate']['connected'] = True
        self.bench.update('climate', {'fw': 'hardware-test-2', 'water_enabled': 0, 'water_wet': -1, 'armed': 0})

    def test_mist_independent_of_water_but_requires_manual_confirmation(self):
        with self.assertRaises(ValueError):
            self.bench.command('climate', 'mist')
        self.assertTrue(self.link.out.empty())
        # Water is disabled/unconnected; manually preparing mist must still work.
        self.bench.command('climate', 'arm', True)
        self.assertEqual(self.link.out.get_nowait(), 'ARM 1')
        self.bench.latest['climate']['armed'] = 1
        for water in (-1, 0, 1):
            self.bench.latest['climate']['water_wet'] = water
            self.bench.command('climate', 'mist', 100000)
            self.assertEqual(self.link.out.get_nowait(), 'MIST 1000')
        self.bench.command('climate', 'water', False)
        self.assertEqual(self.link.out.get_nowait(), 'WATER 0')
        self.bench.received['climate'] = time.time() - 10
        for action in ('arm', 'mist'):
            with self.assertRaises(ValueError):
                self.bench.command('climate', action, True)
        self.assertTrue(self.link.out.empty())

    def test_wrong_firmware_and_invalid_pins_rejected(self):
        self.bench.latest['climate']['fw'] = 'bringup-4'
        with self.assertRaises(ValueError): self.bench.command('climate', 'mist')
        self.bench.latest['climate']['fw'] = 'hardware-test-2'
        with self.assertRaises(ValueError): self.bench.command('climate', 'pins', '5,25')

    def test_nonfinite_readings_and_events_do_not_become_measurements(self):
        self.bench.update('climate', {'fw':'hardware-test-2', 't':float('nan'), 'lux':float('inf')})
        self.bench.update('climate', {'event':'rejected', 'reason':'no water'})
        result = self.bench.snapshot()
        self.assertIsNone(result['devices']['climate']['data']['t'])
        self.assertIsNone(result['devices']['climate']['data']['lux'])
        self.assertIn('fw', result['devices']['climate']['data'])
        json.dumps(result, allow_nan=False)

    def test_partial_jpeg_is_not_saved_or_reported_as_photo(self):
        link = hw.Link(self.bench, 'camera')
        link.stop.set()
        with self.assertRaises(ValueError): link.read_jpeg(io.BytesIO(b'bad'), 'JPEG_BEGIN 100')
        self.assertIsNone(self.bench.jpeg)
        data=b'\xff\xd8'+b'x'*100+b'\xff\xd9'
        link.stop.clear()
        with tempfile.TemporaryDirectory() as folder, patch.object(hw,'ROOT',Path(folder)):
            link.read_jpeg(io.BytesIO(data), 'JPEG_BEGIN '+str(len(data)))
            self.assertEqual((Path(folder)/'data/hardware_test/latest.jpg').read_bytes(),data)
        self.assertEqual(self.bench.jpeg,data)

if __name__ == '__main__': unittest.main()
