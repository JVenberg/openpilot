import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from openpilot.sunnypilot.sunnydrive.sunnydrived import SunnydriveServer


class SunnydriveApiTest(unittest.TestCase):
  def test_api_only(self):
    server = SunnydriveServer(("127.0.0.1", 0))
    server.telemetry = {"timestampMs": 123}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
      base = f"http://127.0.0.1:{server.server_address[1]}"
      request = Request(base + "/telemetry", headers={"Origin": "https://ai.sunnypilot.sunnydrive"})
      with urlopen(request, timeout=2) as response:
        self.assertEqual(json.load(response), server.telemetry)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://ai.sunnypilot.sunnydrive")
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
