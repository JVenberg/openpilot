"""Runs the closed-loop launch scenarios on the patched tree (and stock, if given) and enforces limits."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "harness.py"
SUBMODULES = ("", "opendbc_repo", "msgq_repo", "rednose_repo")

# Absolute limits for every scenario
MIN_GAP = 2.5  # m, "close start" begins at 3.0 m by design
MIN_TTC = 2.0  # s
MAX_JERK = 3.5  # m/s^3

# Limits relative to stock
MAX_GAP_LOSS = 1.5  # m
MAX_EXTRA_DECEL = 1.0  # m/s^2
MIN_LAUNCH_SPEEDUP = 0.3  # s

NORMAL_LAUNCH = "normal launch (lead 1.5 m/s2)"
NORMAL_LAUNCH_MAX_T = 2.4  # s, includes the car's ~0.8 s launch lag
LAUNCHES = {NORMAL_LAUNCH, "gentle lead (0.7 m/s2)", "aggressive lead (3.0 m/s2)", "relaxed personality",
            "aggressive personality", "radar delay 0.3s", "experimental mode, normal launch", "close start (3m) then lead goes"}
FALSE_STARTS = {"radar noise+spikes, lead never moves", "big radar spikes (2 m/s), lead never moves",
                "lead inches forward then stops", "lead rolls back toward us"}
MAX_FALSE_START_CREEP = 1.0  # m
MUST_NOT_MOVE = {"experimental mode, model says stop (red light)"}


def run(root):
  root = Path(root).resolve()
  env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(root / s) for s in SUBMODULES), PARAMS_ROOT=tempfile.mkdtemp())
  out = subprocess.run([sys.executable, str(HARNESS)], env=env, cwd=root, check=True, stdout=subprocess.PIPE, text=True).stdout
  return {r["scenario"]: r for r in json.loads(out)}


def check(name, p, s):
  errs = []

  def need(ok, msg):
    if not ok:
      errs.append(msg)

  need(not p["crash"], "crash")
  need(p["fcw"] == 0, f"fcw={p['fcw']}")
  need(p["min_gap"] >= MIN_GAP, f"min_gap {p['min_gap']:.2f} < {MIN_GAP}")
  need(p["min_ttc"] is None or p["min_ttc"] >= MIN_TTC, f"min_ttc {p['min_ttc']} < {MIN_TTC}")
  need(p["max_jerk"] <= MAX_JERK, f"max_jerk {p['max_jerk']:.2f} > {MAX_JERK}")
  if name in FALSE_STARTS:
    need(p["creep_distance"] <= MAX_FALSE_START_CREEP, f"false start creep {p['creep_distance']:.2f} m")
  if name in MUST_NOT_MOVE:
    need(p["creep_distance"] <= 0.05, f"moved {p['creep_distance']:.2f} m")
  if name == NORMAL_LAUNCH:
    need(p["t_move"] is not None and p["t_move"] <= NORMAL_LAUNCH_MAX_T, f"t_move {p['t_move']} > {NORMAL_LAUNCH_MAX_T}")
  if s is not None:
    need(p["min_gap"] >= s["min_gap"] - MAX_GAP_LOSS, f"min_gap {p['min_gap']:.2f} vs stock {s['min_gap']:.2f}")
    need(p["max_decel"] >= s["max_decel"] - MAX_EXTRA_DECEL, f"max_decel {p['max_decel']:.2f} vs stock {s['max_decel']:.2f}")
    if name in LAUNCHES:
      need(p["t_move"] is not None and s["t_move"] is not None and p["t_move"] <= s["t_move"] - MIN_LAUNCH_SPEEDUP,
           f"t_move {p['t_move']} not {MIN_LAUNCH_SPEEDUP}s faster than stock {s['t_move']}")
  return errs


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--patched", required=True)
  ap.add_argument("--stock")
  args = ap.parse_args()

  patched = run(args.patched)
  stock = run(args.stock) if args.stock else {}
  fmt = lambda v: "-" if v is None else f"{v:.2f}"
  cols = ("t_move", "min_gap", "min_ttc", "max_decel", "max_jerk", "creep_distance")
  print(f"{'scenario (stock/patched)':48}" + "".join(f"{c:>16}" for c in cols))
  failed = False
  for name, p in patched.items():
    s = stock.get(name)
    print(f"{name[:48]:48}" + "".join(f"{(fmt(s[c]) if s else '') + '/' + fmt(p[c]):>16}" for c in cols))
    for e in check(name, p, s):
      print(f"  FAIL: {e}")
      failed = True
  missing = (LAUNCHES | FALSE_STARTS | MUST_NOT_MOVE) - patched.keys()
  if missing:
    print(f"FAIL: missing scenarios {sorted(missing)}")
    failed = True
  print("FAILED" if failed else "PASSED")
  sys.exit(1 if failed else 0)


if __name__ == "__main__":
  main()
