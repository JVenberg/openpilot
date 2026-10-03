import sys
import types

import pytest

# micd only uses messaging at runtime; msgq needs the device build
sys.modules.setdefault("openpilot.cereal.messaging", types.ModuleType("messaging"))
from openpilot.system import micd


class FakeSD:
  def __init__(self, fail_count):
    self.fail_count = fail_count
    self.calls = 0

  def _terminate(self):
    pass

  def _initialize(self):
    self.calls += 1
    if self.calls <= self.fail_count:
      raise OSError("no audio device")

  def InputStream(self, **kwargs):
    return "stream"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
  monkeypatch.setattr("openpilot.common.utils.time.sleep", lambda s: None)


def test_opens_after_slow_device():
  # 12 failures x 3s = 36s, past the old 30s limit
  sd = FakeSD(fail_count=12)
  assert micd.Mic.__new__(micd.Mic).get_stream(sd) == "stream"
  assert sd.calls == 13


def test_gives_up_after_60s():
  sd = FakeSD(fail_count=100)
  with pytest.raises(Exception, match="get_stream failed after retry"):
    micd.Mic.__new__(micd.Mic).get_stream(sd)
  assert sd.calls == 20
