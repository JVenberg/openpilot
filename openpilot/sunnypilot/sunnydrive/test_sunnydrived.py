import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openpilot.sunnypilot.sunnydrive import sunnydrived
from openpilot.sunnypilot.sunnydrive.sunnydrived import SunnydriveServer


class SunnydriveApiTest(unittest.TestCase):
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


if __name__ == "__main__":
  unittest.main()
