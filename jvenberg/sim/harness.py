"""Closed-loop launch sim: real planner -> real LongControl -> real Toyota CarController -> simple PCM/vehicle model.

Imports whichever sunnypilot tree is on PYTHONPATH, so the same scenarios run on stock and patched code.
"""
import json
import math
import sys
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

import numpy as np

from openpilot.cereal import log, messaging
from openpilot.common.realtime import DT_CTRL, DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongControl, LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner
from openpilot.selfdrive.controls.radard import _LEAD_ACCEL_TAU
from opendbc.car import structs
from opendbc.car.toyota.carcontroller import CarController
from opendbc.car.toyota.interface import CarInterface
from opendbc.car.toyota.values import CAR, DBC

CAR_MODEL = CAR.TOYOTA_RAV4_PRIME
PLANNER_EVERY = round(DT_MDL / DT_CTRL)
PCM_DELAY = 0.05  # s, hybrid
PCM_TAU = 0.3  # s
BRAKE_HOLD_RELEASE = 0.8  # s from PERMIT_BRAKING clearing to rolling, measured on a 2021 RAV4 Prime


@dataclass
class Lead:
  x: float
  v: float = 0.
  present: bool = True


@dataclass
class Scenario:
  name: str
  duration: float
  lead_accel: Callable[[float, Lead], float]  # t since lead release -> accel
  events: Callable[[float, "Sim"], None] = lambda t, sim: None
  initial_gap: float | None = None  # None: approach a stopped lead from 10 m/s and let the planner choose the gap
  personality: int = log.LongitudinalPersonality.standard
  e2e: bool = False
  model_stop: bool = False  # e2e model says stop (e.g. red light)
  radar_delay: float = 0.
  radar_noise: Callable[[float, np.random.Generator], float] = lambda t, rng: 0.
  seed: int = 0


class Sim:
  def __init__(self, sc: Scenario):
    self.sc = sc
    self.rng = np.random.default_rng(sc.seed)
    self.CP = CarInterface.get_non_essential_params(CAR_MODEL)
    self.CP_SP = CarInterface.get_non_essential_params_sp(self.CP, CAR_MODEL)
    self.planner = LongitudinalPlanner(self.CP, self.CP_SP, init_v=0.)
    self.loc = LongControl(self.CP, self.CP_SP)
    self.cc = CarController(DBC[CAR_MODEL], self.CP, self.CP_SP)
    self.cc.secoc_key = b'\x00' * 16

    self.x = self.v = self.a = 0.
    self.a_target, self.should_stop = 0., False
    self.t, self.hold_released_at = 0., None
    self.pcm_hist = deque([0.] * max(1, round(PCM_DELAY / DT_CTRL)))
    self.radar_hist = deque(maxlen=max(1, round(sc.radar_delay / DT_MDL)) + 1)
    self.lead = Lead(x=sc.initial_gap if sc.initial_gap is not None else 60.)
    if sc.initial_gap is None:
      self.v = 10.

  # --- radar ---
  def radar_lead(self, t_rel):
    self.radar_hist.append((self.lead.x - self.x, self.lead.v, self.lead.present))
    d, v, present = self.radar_hist[0]
    v += self.sc.radar_noise(t_rel, self.rng)
    return d, v, present

  def planner_step(self, t_rel, cruise_standstill):
    d, v_lead, present = self.radar_lead(t_rel)
    lead = log.RadarState.LeadData.new_message()
    lead.dRel, lead.yRel, lead.vRel = float(d), 0., float(v_lead - self.v)
    lead.vLead = lead.vLeadK = float(v_lead)
    lead.aLeadK, lead.aLeadTau = 0., float(_LEAD_ACCEL_TAU)
    lead.present, lead.modelProb, lead.radar = present, 1.0 if present else 0., True
    radar = messaging.new_message('radarState')
    radar.radarState.leadOne = lead

    model = messaging.new_message('modelV2')
    T = np.array(ModelConstants.T_IDXS)
    v_model = 0. if self.sc.model_stop else self.v + 0.5
    model.modelV2.position.x = [float(x) for x in v_model * T]
    model.modelV2.velocity.x = [float(self.v)] + [float(v_model)] * (len(T) - 1)
    model.modelV2.acceleration.x = [0.] * len(T)
    model.modelV2.orientationRate.z = [0.] * len(T)
    model.modelV2.action.desiredAcceleration = -1.0 if self.sc.model_stop else float(self.a + 0.5)
    model.modelV2.action.shouldStop = self.sc.model_stop
    model.modelV2.meta.disengagePredictions.gasPressProbs = [1.] * 6

    cs = messaging.new_message('carState')
    cs.carState.vEgo, cs.carState.aEgo = float(self.v), float(self.a)
    cs.carState.standstill = self.v < 0.01
    cs.carState.vCruise = cs.carState.vCruiseCluster = 50. * 3.6
    ctl = messaging.new_message('controlsState')
    ctl.controlsState.longControlState = self.loc.long_control_state
    ss = messaging.new_message('selfdriveState')
    ss.selfdriveState.experimentalMode, ss.selfdriveState.personality = self.sc.e2e, self.sc.personality
    ss.selfdriveState.enabled = True
    ccm = messaging.new_message('carControl')
    ccm.carControl.enabled = True
    ccm.carControl.orientationNED = [0., 0., 0.]
    sm = {'radarState': radar.radarState, 'carState': cs.carState, 'carControl': ccm.carControl,
          'controlsState': ctl.controlsState, 'selfdriveState': ss.selfdriveState,
          'vehicleParameters': messaging.new_message('vehicleParameters').vehicleParameters,
          'modelV2': model.modelV2, 'carStateSP': messaging.new_message('carStateSP').carStateSP,
          'liveMapDataSP': messaging.new_message('liveMapDataSP').liveMapDataSP,
          'gpsLocation': messaging.new_message('gpsLocation').gpsLocation}
    self.planner.update(sm)
    self.a_target, self.should_stop = float(self.planner.output_a_target), bool(self.planner.output_should_stop)

  def control_step(self):
    CS = structs.CarState(vEgo=self.v, vEgoRaw=self.v, aEgo=self.a, standstill=self.v < 0.01)
    CS.cruiseState.enabled = True
    accel = self.loc.update(True, CS, self.a_target, self.should_stop, [-3.5, 2.0])

    cc = structs.CarControl(enabled=True, longActive=True, latActive=False)
    cc.actuators.accel = float(accel)
    cc.actuators.longControlState = self.loc.long_control_state
    cc.orientationNED = [0., 0., 0.]
    cc.hudControl.leadDistanceBars = 2
    CSw = SimpleNamespace(out=CS, acc_type=1, gvc=self.a, lkas_hud={}, pcm_acc_status=8, pcm_follow_distance=3,
                          secoc_synchronization={'RESET_CNT': 0, 'TRIP_CNT': 0, 'AUTHENTICATOR': 0},
                          cruiseState=CS.cruiseState)
    self.cc.update(cc.as_reader(), structs.CarControlSP(), CSw, 0)
    return float(self.cc.accel), bool(self.cc.permit_braking)

  def vehicle_step(self, pcm_cmd, permit_braking):
    self.t += DT_CTRL
    self.pcm_hist.append(pcm_cmd)
    cmd = self.pcm_hist.popleft()
    if self.v <= 0.:
      if permit_braking or cmd <= 0.:
        self.hold_released_at = None
      elif self.hold_released_at is None:
        self.hold_released_at = self.t
      if self.hold_released_at is None or self.t - self.hold_released_at < BRAKE_HOLD_RELEASE:
        self.v, self.a = 0., 0.  # brake hold at standstill
        return
    self.a += (cmd - self.a) * DT_CTRL / PCM_TAU
    self.v = max(0., self.v + self.a * DT_CTRL)
    if self.v == 0.:
      self.a = 0.
    self.x += self.v * DT_CTRL

  def run(self):
    sc = self.sc
    # settle phase: approach (or sit behind) the stopped lead
    frames = 0
    while frames * DT_CTRL < 20. and not (self.v == 0. and frames * DT_CTRL > 3. and self.loc.long_control_state == LongCtrlState.stopping):
      if frames % PLANNER_EVERY == 0:
        self.planner_step(-1., False)
      pcm, pb = self.control_step()
      self.vehicle_step(pcm, pb)
      frames += 1
    for _ in range(round(2.0 / DT_CTRL)):  # sit stopped a bit
      if frames % PLANNER_EVERY == 0:
        self.planner_step(-1., False)
      self.vehicle_step(*self.control_step())
      frames += 1

    stopped_gap = self.lead.x - self.x
    x0 = self.x
    m = dict(scenario=sc.name, stopped_gap=stopped_gap, t_move=None, min_gap=math.inf, min_ttc=math.inf,
             max_decel=0., max_jerk=0., crash=False, fcw=0, creep_distance=0.)
    prev_a, prev_v = self.a, self.v
    for i in range(round(sc.duration / DT_CTRL)):
      t = i * DT_CTRL
      sc.events(t, self)
      self.lead.v = max(-1., self.lead.v + sc.lead_accel(t, self.lead) * DT_CTRL)
      self.lead.x += self.lead.v * DT_CTRL
      if i % PLANNER_EVERY == 0:
        self.planner_step(t, False)
        m['fcw'] += int(self.planner.fcw)
      self.vehicle_step(*self.control_step())

      gap = self.lead.x - self.x
      if self.lead.present:
        m['min_gap'] = min(m['min_gap'], gap)
        closing = self.v - self.lead.v
        if closing > 0.1:
          m['min_ttc'] = min(m['min_ttc'], gap / closing)
        m['crash'] |= gap < 0.4
      if m['t_move'] is None and self.v > 0.5:
        m['t_move'] = t
      m['max_decel'] = min(m['max_decel'], self.a)
      if self.v > 0.05 and prev_v > 0.05:
        m["max_jerk"] = max(m["max_jerk"], abs(self.a - prev_a) / DT_CTRL)
      prev_a, prev_v = self.a, self.v
    m['creep_distance'] = self.x - x0
    m['final_speed'] = self.v
    return m


# ---------------- scenarios ----------------
def const(a, v_max=15.):
  return lambda t, L: a if L.v < v_max else 0.

def phases(*segs):
  """segs: (t_end, accel) pairs, then hold 0."""
  def f(t, L):
    for t_end, a in segs:
      if t < t_end:
        return a if (a > 0 or L.v > 0) else 0.
    return 0.
  return f

def cut_in(at, gap, v):
  def ev(t, sim):
    if abs(t - at) < DT_CTRL / 2:
      sim.lead = Lead(x=sim.x + gap, v=v)
  return ev

def lead_turns_off(at, next_stopped_gap):
  def ev(t, sim):
    if abs(t - at) < DT_CTRL / 2:
      sim.lead = Lead(x=sim.x + next_stopped_gap, v=0.)
  return ev

def flicker(period=1.0, off=0.2):
  def ev(t, sim):
    sim.lead.present = (t % period) >= off
  return ev

def spikes(sigma=0.25, spike=1.0, every=3.0, width=0.3):
  return lambda t, rng: float(rng.normal(0, sigma)) + (spike if t >= 0 and (t % every) < width else 0.)


P = log.LongitudinalPersonality
SCENARIOS = [
  Scenario("normal launch (lead 1.5 m/s2)", 12., const(1.5)),
  Scenario("gentle lead (0.7 m/s2)", 15., const(0.7)),
  Scenario("aggressive lead (3.0 m/s2)", 10., const(3.0)),
  Scenario("relaxed personality", 12., const(1.5), personality=P.relaxed),
  Scenario("aggressive personality", 12., const(1.5), personality=P.aggressive),
  Scenario("lead inches forward then stops", 10., phases((1.0, 1.0), (1.4, -3.0))),
  Scenario("lead creeps 0.6 m/s then stops", 10., phases((0.6, 1.0), (4.0, 0.), (4.3, -3.0))),
  Scenario("lead launches, panic stop after 1s", 10., phases((1.0, 2.0), (3.0, -6.0))),
  Scenario("lead launches, panic stop after 2s", 10., phases((2.0, 2.0), (4.0, -6.0))),
  Scenario("lead launches, panic stop after 3s", 12., phases((3.0, 2.0), (6.0, -6.0))),
  Scenario("cut-in 4m ahead at 1 m/s during launch", 12., const(1.5), cut_in(2.5, 4.0, 1.0)),
  Scenario("stopped car cuts in 7m ahead during launch", 12., const(1.5), cut_in(2.0, 7.0, 0.0)),
  Scenario("lead turns off, stopped queue 15m ahead", 12., const(1.5), lead_turns_off(2.5, 15.0)),
  Scenario("lead turns off, stopped queue 9m ahead", 12., const(1.5), lead_turns_off(2.0, 9.0)),
  Scenario("radar noise+spikes, lead never moves", 15., lambda t, L: 0., radar_noise=spikes()),
  Scenario("big radar spikes (2 m/s), lead never moves", 15., lambda t, L: 0., radar_noise=spikes(0.3, 2.0, 2.0, 0.5)),
  Scenario("radar delay 0.3s", 12., const(1.5), radar_delay=0.3),
  Scenario("lead flickers in/out", 12., const(1.5), flicker()),
  Scenario("lead creeps at 0.5 m/s steadily", 10., lambda t, L: 1.0 if L.v < 0.5 else 0.),
  Scenario("lead rolls back toward us", 8., lambda t, L: -1.0 if t < 0.5 else (1.0 if t < 1.0 else 0.)),
  Scenario("experimental mode, model says stop (red light)", 10., const(1.5), e2e=True, model_stop=True),
  Scenario("experimental mode, normal launch", 12., const(1.5), e2e=True),
  Scenario("close start (3m) then lead goes", 10., const(1.5), initial_gap=3.0),
  Scenario("stop-and-go wave 60s", 60., lambda t, L: 1.2 * math.sin(t * 0.6) if (L.v > 0 or t % 10.5 < 5.2) else 0.),
]


if __name__ == "__main__":
  names = set(sys.argv[1:])
  results = [Sim(sc).run() for sc in SCENARIOS if not names or sc.name in names]
  clean = [{k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in r.items()} for r in results]
  print(json.dumps(clean))
