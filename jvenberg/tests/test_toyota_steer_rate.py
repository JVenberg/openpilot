"""Drives the real Toyota CarController for a RAV4 Prime through fast wheel spins."""
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st

import opendbc.car.toyota.carcontroller as mod
from opendbc.car import structs
from opendbc.car.toyota.carcontroller import CarController, MAX_STEER_RATE, STEER_RATE_RELEASE_FRAMES
from opendbc.car.toyota.interface import CarInterface
from opendbc.car.toyota.values import CAR, DBC, CarControllerParams

CP = CarInterface.get_non_essential_params(CAR.TOYOTA_RAV4_PRIME)
CP_SP = CarInterface.get_non_essential_params_sp(CP, CAR.TOYOTA_RAV4_PRIME)
LIMITS = CarControllerParams(CP)
STEER_LKA = 0x2E4


def make_cs(rate, eps_torque, v_ego=4.):
  out = structs.CarState(vEgo=v_ego, vEgoRaw=v_ego, steeringRateDeg=rate, steeringTorqueEps=eps_torque)
  out.cruiseState.enabled = True
  return SimpleNamespace(out=out, acc_type=1, gvc=0., lkas_hud={}, pcm_acc_status=8, pcm_follow_distance=3,
                         secoc_synchronization={'RESET_CNT': 0, 'TRIP_CNT': 0, 'AUTHENTICATOR': 0},
                         cruiseState=out.cruiseState)


def make_cc(torque, lat_active=True):
  cc = structs.CarControl(enabled=True, latActive=lat_active)
  cc.actuators.torque = torque
  cc.orientationNED = [0., 0., 0.]
  cc.hudControl.leadDistanceBars = 2
  return cc.as_reader()


def new_cc():
  c = CarController(DBC[CAR.TOYOTA_RAV4_PRIME], CP, CP_SP)
  c.secoc_key = b'\x00' * 16
  return c


def step(c, torque, rate, lat_active=True):
  """Returns (STEER_REQUEST, STEER_TORQUE_CMD) as sent on the bus; EPS is assumed to track the command."""
  _, sends = c.update(make_cc(torque, lat_active), structs.CarControlSP(), make_cs(rate, c.last_torque), 0)
  dat = next(m[1] for m in sends if m[0] == STEER_LKA)
  return dat[0] & 1, int.from_bytes(dat[1:3], 'big', signed=True)


class PandaSteerReqCheck:
  """Port of the request-bit tolerance in opendbc/safety/lateral.h with Toyota's limits."""
  MIN_VALID, MAX_INVALID = 17, 1

  def __init__(self):
    self.valid = self.invalid = 0
    self.last = 0

  def ok(self, req, torque):
    violation = not min(self.last, 0) - LIMITS.STEER_DELTA_UP <= torque <= max(self.last, 0) + LIMITS.STEER_DELTA_UP
    # stricter than panda: the EPS faults on fast unwinds, so never drop more than STEER_DELTA_DOWN
    violation |= torque * self.last >= 0 and abs(self.last) - abs(torque) > LIMITS.STEER_DELTA_DOWN
    if req == 0 and torque != 0:
      violation |= self.valid < self.MIN_VALID if self.invalid == 0 else self.invalid >= self.MAX_INVALID
      self.valid, self.invalid = 0, min(self.invalid + 1, self.MAX_INVALID)
    else:
      self.valid, self.invalid = min(self.valid + 1, self.MIN_VALID), 0
    self.last = torque
    return not violation


def test_releases_torque_during_fast_spin():
  c = new_cc()
  for _ in range(200):
    step(c, 1., 0.)
  frames = [step(c, 1., 250.) for _ in range(100)]
  released = next(i for i, (req, tq) in enumerate(frames) if req == 0 and tq == 0)
  assert released <= LIMITS.STEER_MAX // LIMITS.STEER_DELTA_DOWN + 1
  assert all(f == (0, 0) for f in frames[released:])


def test_resumes_after_wheel_slows():
  c = new_cc()
  for _ in range(100):
    step(c, 1., 250.)
  frames = [step(c, 1., 20.) for _ in range(STEER_RATE_RELEASE_FRAMES + 20)]
  assert all(f == (0, 0) for f in frames[:STEER_RATE_RELEASE_FRAMES - 1])
  assert frames[-1][0] == 1 and frames[-1][1] > 0


def test_unchanged_below_rate_limit(monkeypatch):
  patched, upstream = new_cc(), new_cc()
  for i in range(500):
    torque, rate = (1. if i % 200 < 100 else -1.), (MAX_STEER_RATE - 1) * (-1) ** (i // 50)
    a = step(patched, torque, rate)
    monkeypatch.setattr(mod, "STEER_RATE_RELEASE_FRAMES", 0)
    assert step(upstream, torque, rate) == a
    monkeypatch.undo()


STEP = st.tuples(st.floats(-1., 1.), st.floats(-400., 400.), st.booleans())


@settings(max_examples=2000, deadline=None)
@given(st.lists(st.tuples(STEP, st.integers(1, 60)), min_size=1, max_size=20))
def test_fuzz_panda_safety(seq):
  c, panda = new_cc(), PandaSteerReqCheck()
  for (torque, rate, lat_active), n in seq:
    for _ in range(n):
      req, tq = step(c, torque, rate, lat_active)
      if not lat_active:
        panda = PandaSteerReqCheck()
        assert tq == 0
        continue
      assert panda.ok(req, tq), (req, tq, panda.last)
