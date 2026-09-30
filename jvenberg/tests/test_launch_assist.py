"""Tests get_launch_assist_accel using the real source from the repo (extracted via ast, no acados needed)."""
import ast
import itertools
import math
import pathlib
from types import SimpleNamespace

import numpy as np
from hypothesis import given, settings, strategies as st

REPO = pathlib.Path(__file__).resolve().parents[2] / "openpilot"
PLANNER = REPO / "selfdrive/controls/lib/longitudinal_planner.py"
MPC = REPO / "selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py"


class Personality:
  relaxed, standard, aggressive = 0, 1, 2


def extract(path, names, ns):
  tree = ast.parse(path.read_text())
  keep = [n for n in tree.body
          if (isinstance(n, ast.FunctionDef) and n.name in names)
          or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets))]
  exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)


ns = {"np": np, "math": math, "log": SimpleNamespace(LongitudinalPersonality=Personality)}
extract(MPC, {"COMFORT_BRAKE", "STOP_DISTANCE", "get_T_FOLLOW", "get_stopped_equivalence_factor",
              "get_safe_obstacle_distance"}, ns)
extract(PLANNER, {"LAUNCH_ASSIST_ACCEL_BP", "LAUNCH_ASSIST_ACCEL_V", "LAUNCH_ASSIST_MIN_V_REL",
                  "LAUNCH_ASSIST_GAP_MARGIN", "get_launch_assist_accel"}, ns)
assist = ns["get_launch_assist_accel"]
STOP_DISTANCE = ns["STOP_DISTANCE"]
PERSONALITIES = (Personality.relaxed, Personality.standard, Personality.aggressive)
MAX_ASSIST = max(ns["LAUNCH_ASSIST_ACCEL_V"])


def lead(status=True, d=10., v=2.):
  return SimpleNamespace(status=status, dRel=d, vLead=v)


def check_output(a):
  assert a is None or (isinstance(a, float) and math.isfinite(a) and 0. <= a <= MAX_ASSIST), a


def test_grid_bounds_and_gating():
  vs = np.concatenate([np.linspace(-1, 5, 61), [10, 20, 40]])
  vleads = np.concatenate([np.linspace(-5, 5, 41), [10, 30]])
  ds = np.concatenate([np.linspace(0, 12, 49), [20, 50, 150]])
  n_active = 0
  for v, vl, d, st_, p in itertools.product(vs, vleads, ds, (True, False), PERSONALITIES):
    a = assist(lead(st_, d, vl), float(v), p)
    check_output(a)
    if a is None:
      continue
    n_active += 1
    assert st_, "fired without a lead"
    assert v < 3.0, "fired at speed"
    assert vl - v >= 0.5, "fired while lead not pulling away"
    vc, vlc = max(v, 0.), min(vl, 13.)
    assert d >= STOP_DISTANCE - 0.5 - 1e-9 - vlc ** 2 / 5 + vc ** 2 / 5 + 1.25 * vc, "fired inside safe gap"
  assert n_active > 0


def test_never_fires_when_close_at_standstill():
  for vl in np.linspace(0.5, 3., 26):
    for d in np.linspace(0, 5.0, 51):
      assert assist(lead(d=d, v=vl), 0., Personality.standard) is None or d >= STOP_DISTANCE - 0.5 - vl ** 2 / 5


def test_fires_on_typical_launch():
  # stopped ~6.5 m behind a lead that has started pulling away at 1 m/s
  for p in PERSONALITIES:
    assert assist(lead(d=6.5, v=1.0), 0., p) == 1.0


def test_monotonic_taper_with_speed():
  prev = math.inf
  for v in np.linspace(0, 3.2, 100):
    a = assist(lead(d=100., v=v + 5), float(v), Personality.standard)
    a = 0. if a is None else a
    assert a <= prev + 1e-12
    prev = a


def test_non_finite_inputs_are_ignored():
  bad = (math.nan, math.inf, -math.inf)
  for x in bad:
    assert assist(lead(d=x), 0., Personality.standard) is None
    assert assist(lead(v=x), 0., Personality.standard) is None
    assert assist(lead(), x, Personality.standard) is None


@settings(max_examples=20000, deadline=None)
@given(st.booleans(), st.floats(allow_nan=True, allow_infinity=True), st.floats(allow_nan=True, allow_infinity=True),
       st.floats(allow_nan=True, allow_infinity=True), st.sampled_from(PERSONALITIES))
def test_fuzz(status, d, vl, v, p):
  check_output(assist(lead(status, d, vl), v, p))


def simulate(lead_accel_profile, d0=6.2, dt=0.05, T=12., personality=Personality.standard):
  """Ego follows only the assist (else holds/brakes), lead follows profile. Returns min gap and ego peak speed."""
  x_e = v_e = 0.
  x_l, v_l = d0, 0.
  min_gap = d0
  for i in range(int(T / dt)):
    t = i * dt
    a_l = lead_accel_profile(t, v_l)
    v_l = max(0., v_l + a_l * dt)
    x_l += v_l * dt
    a = assist(lead(d=x_l - x_e, v=v_l), v_e, personality)
    a_e = a if a is not None else (-3.5 if v_e > 0 else 0.)  # worst case: nothing else helps
    v_e = max(0., v_e + a_e * dt)
    x_e += v_e * dt
    min_gap = min(min_gap, x_l - x_e)
  return min_gap


def test_closed_loop_lead_stops_abruptly():
  # lead creeps forward then slams on brakes at various times; assist alone must never close the gap
  for t_stop in np.linspace(0.3, 4, 20):
    for a_go in (0.5, 1.0, 2.0, 3.0):
      prof = lambda t, v, t_stop=t_stop, a_go=a_go: a_go if t < t_stop else -5.0
      assert simulate(prof) > 4.0, (t_stop, a_go)


def test_closed_loop_stop_and_go_wave():
  prof = lambda t, v: 1.5 * math.sin(t * 1.3)
  assert simulate(prof, T=30.) > 4.0
