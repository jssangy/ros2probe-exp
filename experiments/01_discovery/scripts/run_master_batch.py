import argparse, os, subprocess, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("--scale", choices=["G1", "G3", "G5"], required=True)
parser.add_argument("--condition", choices=["baseline", "rp_run", "ros2_daemon"], required=True)
parser.add_argument("--rep", type=int, required=True)
parser.add_argument("--N_P", type=int, choices=[1, 3, 5], required=True)
parser.add_argument("--N_E", type=int, choices=[2], default=2)
parser.add_argument("--results-root", default="results/01_discovery")
args, forwarded = parser.parse_known_args()
if args.N_P != int(args.scale[1:]):
    parser.error('--scale must match --N_P; G1/G3/G5 mean NP=1/3/5 per host')
if args.rep < 1:
    parser.error('--rep must be positive')

runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "master_runner.py")
capture_output = os.path.join(
    args.results_root,
    "{host}",
    args.scale,
    args.condition,
    f"run_{args.rep:02d}",
    "discovery_capture.pcap",
)
cmd = [
    sys.executable,
    runner,
    str(args.N_E),
    str(args.N_P),
    "--capture-output",
    capture_output,
    "--observer",
    args.condition,
]
print(" ", " ".join(cmd))
subprocess.run(cmd + forwarded, check=True)
if "--dry-run" not in forwarded:
    time.sleep(15)
