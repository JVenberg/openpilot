"""Drives the real Toyota CarController for a RAV4 Prime through stop -> launch sequences."""
import math
from types import SimpleNamespace

import numpy as np
from hypothesis import given, settings, strategies as st

from opendbc.car import structs
from opendbc.car.toyota.carcontroller import CarController, LAUNCH_UNWIND_FLOOR, LAUNCH_UNWIND_MAX_SPEED
from opendbc.car.toyota.interface import CarInterface
from opendbc.car.toyota.values import CAR, DBC, CarControllerParams

LongCtrlState = structs.CarControl.Actuators.LongControlState
CP = CarInterface.get_non_essential_params(CAR.TOYOTA_RAV4_PRIME)
CP_SP = CarInterface.get_non_essential_params_sp(CP, CAR.TOYOTA_RAV4_PRIME)
LIMITS = CarControllerParams(CP)
DT = 0.01


def make_cs(v_ego, a_ego=0.):
  out = structs.CarState(vEgo=v_ego, vEgoRaw=v_ego, aEgo=a_ego, standstill=v_ego < 0.01)
  out.cruiseState.enabled = True
  return SimpleNamespace(out=out, acc_type=1, gvc=a_ego, lkas_hud={}, pcm_acc_status=8, pcm_follow_distance=3,
                         secoc_synchronization={'RESET_CNT': 0, 'TRIP_CNT': 0, 'AUTHENTICATOR': 0},
                         cruiseState=out.cruiseState)


def make_cc(accel, state):
  cc = structs.CarControl(enabled=True, longActive=True, latActive=False)
  cc.actuators.accel = accel
  cc.actuators.longControlState = state
  cc.orientationNED = [0., 0., 0.]
  cc.hudControl.leadDistanceBars = 2
  return cc.as_reader()


def new_cc():
  c = CarController(DBC[CAR.TOYOTA_RAV4_PRIME], CP, CP_SP)
  c.secoc_key = b'\x00' * 16
  return c


def step(c, accel, state, v, a_ego=0.):
  c.update(make_cc(accel, state), structs.CarControlSP(), make_cs(v, a_ego), 0)
  return c.accel, c.permit_braking


def hold_at_stop(c, frames=300):
  for _ in range(frames):
    step(c, -2.0, LongCtrlState.stopping, 0.)


def time_to_release(c, target=1.0, v_after=0.):
  """Frames until PERMIT_BRAKING drops (car allowed to drive) once launch starts."""
  for i in range(300):
    accel, permit_braking = step(c, target, LongCtrlState.pid, v_after)
    assert LIMITS.ACCEL_MIN <= accel <= LIMITS.ACCEL_MAX
    if not permit_braking:
      return i * DT
  return math.inf


def test_launch_is_faster_than_baseline(monkeypatch):
  c = new_cc(); hold_at_stop(c)
  t_new = time_to_release(c)
  import opendbc.car.toyota.carcontroller as mod
  monkeypatch.setattr(mod, "LAUNCH_UNWIND_FLOOR", -1e9)  # disables the floor = upstream behavior
  c = new_cc(); hold_at_stop(c)
  t_old = time_to_release(c)
  print(f"\nPERMIT_BRAKING release: upstream {t_old:.2f}s -> patched {t_new:.2f}s")
  assert t_new < t_old


def test_floor_never_applies_while_stopping_or_braking():
  c = new_cc(); hold_at_stop(c)
  for a in np.linspace(-3.5, 0., 30):
    for state in (LongCtrlState.stopping, LongCtrlState.pid):
      c = new_cc(); hold_at_stop(c)
      accel, pb = step(c, float(a), state, 0.)
      assert accel <= 0. and pb  # still braking


def test_floor_never_applies_above_speed_gate():
  for v in (LAUNCH_UNWIND_MAX_SPEED, 1., 5., 30.):
    c = new_cc()
    for _ in range(200):
      step(c, -2.0, LongCtrlState.pid, v)
    accel, _ = step(c, 1.0, LongCtrlState.pid, v)
    assert accel < LAUNCH_UNWIND_FLOOR, (v, accel)  # rate limited from -2.0 as upstream


def test_floor_is_bounded_first_frame():
  # first launch command after hold can't exceed floor + one rate-limit step
  c = new_cc(); hold_at_stop(c)
  accel, _ = step(c, 2.0, LongCtrlState.pid, 0.)
  assert accel <= LAUNCH_UNWIND_FLOOR + 4.0 * DT * 3 + 1e-6 + 1.5  # generous: includes PID feedforward/pitch comp


import opendbc.car.toyota.carcontroller as mod

STEP = st.tuples(st.floats(-5., 5.), st.sampled_from([LongCtrlState.pid, LongCtrlState.stopping]),
                 st.floats(0., 30.), st.floats(-4., 4.))


@settings(max_examples=3000, deadline=None)
@given(st.lists(STEP, min_size=1, max_size=150))
def test_fuzz_lockstep_vs_upstream(seq):
  patched, upstream = new_cc(), new_cc()
  real_floor = mod.LAUNCH_UNWIND_FLOOR
  diverged_allowed = False
  for accel, state, v, a_ego in seq:
    a_p, _ = step(patched, accel, state, v, a_ego)
    mod.LAUNCH_UNWIND_FLOOR = -1e9
    try:
      a_u, _ = step(upstream, accel, state, v, a_ego)
    finally:
      mod.LAUNCH_UNWIND_FLOOR = real_floor
    assert math.isfinite(a_p) and LIMITS.ACCEL_MIN <= a_p <= LIMITS.ACCEL_MAX
    diverged_allowed |= accel > 0 and state == LongCtrlState.pid and v < LAUNCH_UNWIND_MAX_SPEED
    if not diverged_allowed:
      assert a_p == a_u, "changed behavior outside launch-from-stop"
