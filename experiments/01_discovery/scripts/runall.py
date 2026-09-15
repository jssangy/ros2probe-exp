import argparse, os, subprocess, sys, time

parser = argparse.ArgumentParser()
parser.add_argument(
    "--scale",
    choices=["G1", "G3", "G5", "all"],
    default="all",
    help="Scale to run. Default runs all scales.",
)
parser.add_argument(
    "--condition",
    choices=["baseline", "rp_run", "ros2_daemon", "all"],
    default="all",
    help="Condition to run. Default runs all conditions.",
)
parser.add_argument("--repetitions", type=int, default=10)
args, forwarded = parser.parse_known_args()
if args.repetitions < 1:
    parser.error('--repetitions must be positive')

runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_master_batch.py")
all_scales = [("G1", 1), ("G3", 3), ("G5", 5)]
scales = all_scales if args.scale == "all" else [item for item in all_scales if item[0] == args.scale]
conditions = ["baseline", "rp_run", "ros2_daemon"] if args.condition == "all" else [args.condition]

for scale_name, n_p in scales:
    for condition in conditions:
        for rep in range(1, args.repetitions + 1):
            cmd = [
                sys.executable,
                runner,
                "--scale", scale_name,
                "--condition", condition,
                "--rep", str(rep),
                "--N_P", str(n_p),
            ]
            print(" ", " ".join(cmd))
            subprocess.run(cmd + forwarded, check=True)
            if "--dry-run" not in forwarded:
                time.sleep(10)
