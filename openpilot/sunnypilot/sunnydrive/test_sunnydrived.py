import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openpilot.sunnypilot.sunnydrive import sunnydrived
from openpilot.sunnypilot.sunnydrive import pairing
from openpilot.sunnypilot.sunnydrive.sunnydrived import SunnydriveServer


class SunnydriveApiTest(unittest.TestCase):
  class FakeParams:
    def __init__(self):
      self.values = {"DongleId": "comma-test-12345678"}

    def get(self, key):
      return self.values.get(key)

    def put(self, key, value, block=False):
      self.values[key] = value

    def remove(self, key):
      self.values.pop(key, None)

  def test_pairing_methods_share_one_credential(self):
    params = self.FakeParams()
    client = "phone_1234567890123456"
    window = pairing.open_window(params)
    token = pairing.complete_pairing(client, "Phone", window["code"], params)
    self.assertTrue(pairing.authorized(token, params))
    self.assertFalse(pairing.authorized(token + "x", params))

    request = pairing.request_pairing("phone_abcdefghijklmnop", "Second phone", params)
    self.assertTrue(pairing.approve_request(request, params))
    approved = pairing.consume_request(request, params)
    self.assertEqual(approved["status"], "approved")
    self.assertTrue(pairing.authorized(approved["token"], params))
    self.assertEqual(pairing.consume_request(request, params)["status"], "expired")

    locked = pairing.open_window(params)
    for _ in range(5):
      with self.assertRaises(PermissionError):
        pairing.complete_pairing("phone_locked_123456789", "Locked", "wrong", params)
    with self.assertRaises(PermissionError):
      pairing.complete_pairing("phone_locked_123456789", "Locked", locked["code"], params)

  def test_discovery_is_metadata_only(self):
    packet = sunnydrived.DISCOVERY_PREFIX + b'{"v":1,"nonce":"12345678","clientId":"phone_1234567890123456"}'
    with patch.object(pairing, "device_id", return_value="comma-id"), \
         patch.object(pairing, "device_name", return_value="comma test"), \
         patch.object(pairing, "is_paired", return_value=False):
      reply = sunnydrived.discovery_response(packet)
    body = json.loads(reply[len(sunnydrived.DISCOVERY_PREFIX):])
    self.assertEqual(set(body), {"v", "nonce", "deviceId", "name", "httpPort", "apiVersion", "paired"})
    self.assertNotIn("token", body)

  def test_settings_follow_sunnylink_safety_rules(self):
    with patch("openpilot.common.params.Params") as params_class, patch.object(sunnydrived, "_setting_engaged", return_value=False) as engaged:
      params_class.return_value.get_bool.return_value = False
      params_class.return_value.get_type.return_value = 1
      sunnydrived.sunnylink_set("AlphaLongitudinalEnabled", True)
      params_class.return_value.put.assert_called_once()
      with self.assertRaisesRegex(PermissionError, "onroad or engaged"):
        sunnydrived.sunnylink_set("Mads", True)
      engaged.return_value = True
      with self.assertRaisesRegex(PermissionError, "onroad or engaged"):
        sunnydrived.sunnylink_set("AlphaLongitudinalEnabled", True)
      params_class.return_value.put.assert_called_once()
      params_class.return_value.get.return_value = b"1"   # TorqueParamsOverrideEnabled satisfies the schema's onroad alternative
      params_class.return_value.get_type.return_value = 3
      sunnydrived.sunnylink_set("TorqueParamsOverrideFriction", 0.1)
      self.assertEqual(params_class.return_value.put.call_count, 2)
      params_class.return_value.remove.assert_not_called()

  def test_route_list_cache(self):
    with TemporaryDirectory() as folder, patch.object(sunnydrived, "REALDATA", Path(folder)):
      sunnydrived._connect_routes.cache_clear()
      try:
        self.assertEqual(sunnydrived.connect_routes(), [])
        self.assertEqual(sunnydrived.connect_routes(), [])
        self.assertEqual(sunnydrived._connect_routes.cache_info().hits, 1)
      finally:
        sunnydrived._connect_routes.cache_clear()

  def test_api_only(self):
    server = SunnydriveServer(("127.0.0.1", 0))
    server.publish_telemetry({"timestampMs": 123})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
      base = f"http://127.0.0.1:{server.server_address[1]}"
      request = Request(base + "/telemetry", headers={"Origin": "https://ai.sunnypilot.sunnydrive"})
      with urlopen(request, timeout=2) as response:
        self.assertEqual(json.load(response), server.telemetry)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://ai.sunnypilot.sunnydrive")
      with urlopen(base + "/telemetry/stream", timeout=2) as first, urlopen(base + "/telemetry/stream", timeout=2) as second:
        self.assertEqual(first.readline(), b'data: {"timestampMs":123}\n')
        self.assertEqual(second.readline(), b'data: {"timestampMs":123}\n')
        first.readline()
        second.readline()
        server.publish_telemetry({"timestampMs": 456})
        self.assertEqual(first.readline(), b'data: {"timestampMs":456}\n')
        self.assertEqual(second.readline(), b'data: {"timestampMs":456}\n')
      for path in ("/", "/index.html", "/replay", "/youtube-playlists"):
        with self.assertRaises(HTTPError) as error:
          urlopen(base + path, timeout=2)
        self.assertEqual(error.exception.code, 404)
    finally:
      server.shutdown()
      server.server_close()
      thread.join(timeout=2)

  def test_unpaired_phone_cannot_read_api(self):
    server = SunnydriveServer(("127.0.0.1", 0), allow_loopback=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
      base = f"http://127.0.0.1:{server.server_address[1]}"
      with patch.object(pairing, "authorized", return_value=False):
        with self.assertRaises(HTTPError) as error:
          urlopen(base + "/telemetry", timeout=2)
        self.assertEqual(error.exception.code, 401)
      with patch.object(pairing, "device_id", return_value="comma-id"), \
           patch.object(pairing, "device_name", return_value="comma test"), \
           patch.object(pairing, "is_paired", return_value=False):
        with urlopen(base + "/pair/info?client_id=phone_1234567890123456", timeout=2) as response:
          self.assertEqual(json.load(response)["deviceId"], "comma-id")
      with patch.object(pairing, "authorized", return_value=True):
        with urlopen(base + "/telemetry?auth=paired", timeout=2) as response:
          self.assertIn("timestampMs", json.load(response))
    finally:
      server.shutdown()
      server.server_close()
      thread.join(timeout=2)


if __name__ == "__main__":
  unittest.main()
