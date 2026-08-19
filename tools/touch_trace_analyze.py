#!/usr/bin/env python3
"""Turn a touch_trace.py capture into the numbers a touch filter needs.

Reads the #TT CSV blocks the firmware dumps and reports, per touch and in
aggregate:

  press_err   px between the FIRST reported point and the settled position.
              This is the measurement the whole exercise exists for -- it is
              the error in the press origin that LVGL latches and that
              ui_touchfx blooms at.
  settle_idx  how many reports it takes to reach the settled position.
  settle_us   how long that is in real time -- the latency a filter would add.
  hold_jitter px of wander once settled, which sets the agreement tolerance.

Coordinates are the driver's CHANNEL frame (portrait 240x320), pre-rotation.

Usage: python3 tools/touch_trace_analyze.py /tmp/touch_trace.log
"""
import argparse
import math
import re
import sys

ROW = re.compile(r"#TT (\d+),(\d+),(-?\d+),(-?\d+),(\d)")
BEGIN = re.compile(r"#TT-BEGIN seq=(\d+) n=(\d+) sat=(\d)")

CH_X, CH_Y = 240, 320       # driver output frame
SETTLE_WIN = 4              # consecutive agreeing reports = "settled"


def pct(values, q):
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return float(s[lo]) if lo == hi else s[lo] + (s[hi] - s[lo]) * (pos - lo)


def median(values):
    return pct(values, 0.5)


def parse(path):
    traces, cur, sat = [], None, False
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            b = BEGIN.search(line)
            if b:
                cur, sat = [], b.group(3) == "1"
                continue
            if "#TT-END" in line and cur is not None:
                traces.append({"rows": cur, "sat": sat})
                cur = None
                continue
            m = ROW.search(line)
            if m and cur is not None:
                cur.append({
                    "us": int(m.group(2)), "x": int(m.group(3)),
                    "y": int(m.group(4)), "down": int(m.group(5)),
                })
    return traces


def analyse(trace):
    rows = trace["rows"]
    down = [r for r in rows if r["down"]]
    up = [r for r in rows if not r["down"]]
    if len(down) < 4:
        return None

    # Settled position: median of the back half, which is past any transient.
    tail = down[len(down) // 2:]
    px, py = median([r["x"] for r in tail]), median([r["y"] for r in tail])

    def dist(r):
        return math.hypot(r["x"] - px, r["y"] - py)

    hold = [dist(r) for r in tail]
    hold_max = max(hold)
    # Tolerance floor of 3 px so a freakishly clean tail cannot demand that
    # every sample be pixel-identical, plus 1 px of headroom over the p95 so a
    # lone outlier does not redefine "settled".
    tol = max(3.0, pct(hold, 0.95) + 1.0)

    # Settled = the first report followed by SETTLE_WIN consecutive reports
    # that all agree within tol. Deliberately the same rule the filter itself
    # would apply, and unlike "every remaining sample" it does not let one
    # late jitter spike push the index to the end of the trace.
    settle_idx = len(down) - 1
    for k in range(len(down)):
        win = down[k:k + SETTLE_WIN]
        if len(win) < 2:
            break
        if all(dist(r) <= tol for r in win):
            settle_idx = k
            break

    gaps = [b["us"] - a["us"] for a, b in zip(down, down[1:])]
    clamped = sum(1 for r in down
                  if r["x"] <= 0 or r["y"] <= 0
                  or r["x"] >= CH_X - 1 or r["y"] >= CH_Y - 1)

    return {
        "n": len(down),
        "sat": trace["sat"],
        "press_err": dist(down[0]),
        "settle_idx": settle_idx,
        "settle_us": down[settle_idx]["us"],
        "hold_p95": pct(hold, 0.95),
        "hold_max": hold_max,
        "interval_us": median(gaps) if gaps else float("nan"),
        "dur_us": down[-1]["us"],
        "release_gap_us": (up[-1]["us"] - down[-1]["us"]) if up else None,
        "clamped": clamped,
        "first3": [(r["x"], r["y"]) for r in down[:3]],
        "settled": (round(px, 1), round(py, 1)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    args = ap.parse_args()

    traces = parse(args.log)
    if not traces:
        sys.exit("no #TT blocks found -- was the trace build actually flashed?")

    res = [r for r in (analyse(t) for t in traces) if r]
    if not res:
        sys.exit(f"{len(traces)} block(s) found but all too short to analyse.")

    print(f"{len(res)} usable touch(es) of {len(traces)} captured\n")
    print(f"{'#':>3} {'n':>4} {'press_err':>9} {'settle':>6} {'settle_us':>9} "
          f"{'hold_p95':>8} {'iv_us':>6} {'clamp':>5}  first->settled")
    for i, r in enumerate(res):
        print(f"{i:>3} {r['n']:>4} {r['press_err']:>9.1f} "
              f"{r['settle_idx']:>6} {r['settle_us']:>9} "
              f"{r['hold_p95']:>8.1f} {r['interval_us']:>6.0f} "
              f"{r['clamped']:>5}  {r['first3'][0]} -> {r['settled']}"
              + ("  [SATURATED]" if r["sat"] else ""))

    perr = [r["press_err"] for r in res]
    sidx = [r["settle_idx"] for r in res]
    sus = [r["settle_us"] for r in res]
    hold = [r["hold_max"] for r in res]
    iv = [r["interval_us"] for r in res]

    print("\n--- aggregate (median / p95) ---")
    print(f"press origin error : {median(perr):6.1f} / {pct(perr,0.95):6.1f} px")
    print(f"settle index       : {median(sidx):6.1f} / {pct(sidx,0.95):6.1f} reports")
    print(f"settle time        : {median(sus)/1000:6.2f} / {pct(sus,0.95)/1000:6.2f} ms")
    print(f"hold jitter (max)  : {median(hold):6.1f} / {pct(hold,0.95):6.1f} px")
    print(f"report interval    : {median(iv)/1000:6.2f} ms  "
          f"(~{1e6/median(iv):.0f} reports/s)")

    tol = math.ceil(pct(hold, 0.95))
    rep = max(2, math.ceil(pct(sidx, 0.95)))
    lat = pct(sus, 0.95) / 1000.0

    print("\n--- filter constants implied by this capture ---")
    print(f"agreement tolerance : {tol} px")
    print(f"consecutive reports : {rep}")
    print(f"added press latency : {lat:.1f} ms")

    print("\n--- verdict ---")
    if median(perr) >= 8:
        print(f"CONFIRMED: the press origin lands {median(perr):.1f} px from where")
        print("the finger settles. Fixing the origin is the right target.")
    elif median(perr) >= 3:
        print(f"PARTIAL: press origin is off by {median(perr):.1f} px -- real but")
        print("modest. Check hold jitter below before committing to the filter.")
    else:
        print(f"REFUTED: press origin is only {median(perr):.1f} px off. The")
        print("touch-down transient is NOT the problem -- do not build the")
        print("filter. Look at hold jitter and at calibration instead.")


if __name__ == "__main__":
    main()
