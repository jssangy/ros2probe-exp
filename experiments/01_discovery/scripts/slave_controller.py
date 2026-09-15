#!/usr/bin/env python3
# 이 스크립트는 Python 3로 실행됨 (슬레이브 호스트에서만 사용)

"""
슬레이브 호스트에서 실행. 마스터가 넘긴 목표 시각 5초 전에 패킷 캡처를 시작하고,
목표 시각에 discovery_test를 프로세스 개수(N_P)만큼 동시에 실행한다.
패킷 캡처는 슬레이브당 1개(tshark 1개)만 수행하여 pcap 파일이 여러 개 생기지 않도록 한다.
"""

import socket
import shlex

import argparse  # 명령줄 인자 파싱
import csv  # param_id_table 및 변환 CSV 읽기/쓰기
import glob  # topic report 파일 목록
import os  # expanduser, unlink 등
import re  # tshark -V 텍스트 파싱
import shutil  # 파일 복사 (필터 실패 시 raw → 최종 pcap)
import signal  # tshark에 SIGINT 전송
import subprocess  # tshark, ros2 run 등 외부 프로세스 실행
import sys  # 종료 코드
import tempfile  # 임시 pcap 파일 경로 생성
import time  # 현재 시각·대기 (wait_until)
from collections import defaultdict
from typing import Dict, Optional  # 타입 힌트


CAPTURE_EARLY_SEC = 5  # 목표 시각보다 이만큼(초) 전에 캡처 시작
ROS_DOMAIN_ID = os.environ.get("DISCOVERY_ROS_DOMAIN_ID", "0")


def _package_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))


sys.path.insert(0, _package_dir())
from common.discovery import PROTOCOL, local_completion


def wait_until(unix_time: float) -> None:
    """현재 시각이 unix_time(초)이 될 때까지 대기."""
    now = time.time()  # 현재 시각 (Unix timestamp, 초)
    delay = unix_time - now  # 기다려야 할 시간(초)
    if delay > 0:
        time.sleep(delay)  # delay초만큼 대기


def load_ros_env(ros_setup: str, workspace_setup: str, extra_lib_dir: str = "") -> dict:
    """Source ROS setup files and return the resulting environment as a dict.

    Calling this before wait_until(target_time) moves the ~400ms shell sourcing
    overhead out of the timing-critical section so both hosts start simultaneously.
    """
    cmd = f"source {shlex.quote(ros_setup)} && source {shlex.quote(workspace_setup)} && env -0"
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True)
    env: Dict[str, str] = {}
    for item in result.stdout.split('\0'):
        if '=' in item:
            key, _, value = item.partition('=')
            env[key] = value
    if extra_lib_dir:
        env["LD_LIBRARY_PATH"] = extra_lib_dir + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


def run_slave(
    target_time: int, n_e: int, n_p: int, total_processes: int,
    run_duration_sec: int, single_topic: bool, topic_random: bool,
    capture_output: str, capture_interface: str, ros_setup: str,
    workspace_setup: str, fastdds_lib_dir: str = "", observer: str = "baseline",
    observer_lead_sec: int = 10, rp_command: str = "sudo -n rp run",
) -> int:
    import json
    from pathlib import Path
    if n_e != 2 or n_p not in (1, 3, 5) or total_processes < n_p:
        raise ValueError("Paper discovery requires N_E=2 and per-host N_P in 1, 3, 5")
    if observer_lead_sec < 10 or run_duration_sec <= 0:
        raise ValueError("Observer stabilization must be at least 10s; duration must be positive")
    capture_output = capture_output.replace("{host}", socket.gethostname().split(".")[0])
    output = Path(capture_output)
    if not output.is_absolute():
        output = Path(_package_dir()) / output
    folder = output.parent
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError(f"Run directory already contains data; select a new run label: {folder}")
    status = folder / "run_status.txt"
    status.write_text("incomplete\n")
    (folder / "protocol_version.txt").write_text(PROTOCOL + "\n")
    owned = []
    logs = []
    report_dir = tempfile.mkdtemp(prefix="discovery_topic_report_")
    ros_env = load_ros_env(ros_setup, workspace_setup, fastdds_lib_dir)
    ros_env["ROS_DOMAIN_ID"] = ROS_DOMAIN_ID
    ros_env["RMW_IMPLEMENTATION"] = "rmw_fastrtps_cpp"
    ros_env["DISCOVERY_TOPIC_REPORT_DIR"] = report_dir
    rp_bin = ros_env.get("RP_BIN") or shutil.which("rp") or "rp"
    candidates = [Path(prefix) / "lib/rclcpp_discovery_traffic/discovery_test"
                  for prefix in ros_env.get("AMENT_PREFIX_PATH", "").split(os.pathsep) if prefix]
    binary = next((str(candidate) for candidate in candidates if os.access(candidate, os.X_OK)), None)
    if not binary:
        raise FileNotFoundError("Build the discovery workload first")

    def start(command, name, privileged=False):
        log = (folder / name).open("w")
        logs.append(log)
        proc = subprocess.Popen(command, env=ros_env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        owned.append((proc, privileged))
        return proc

    def stop(proc, privileged=False):
        # Each session was created above; never signal unrelated workloads.
        for sig, timeout in ((signal.SIGINT, 8), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            if privileged:
                subprocess.run(["sudo", "-n", "kill", f"-{sig.value}", "--", f"-{proc.pid}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                pass
        raise RuntimeError(f"Could not stop owned process session {proc.pid}")

    try:
        # Clock correction is operator setup, never an unchecked per-run clock step.
        subprocess.run(["chronyc", "waitsync", "1", "0.001"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        subprocess.run(["ros2", "daemon", "stop"], env=ros_env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if subprocess.run(["pgrep", "-x", "discovery_test"], capture_output=True).returncode == 0:
            raise RuntimeError("A discovery workload is already active; stop it in its owning terminal")
        capture_at = target_time - CAPTURE_EARLY_SEC
        observer_at = capture_at - observer_lead_sec - 2
        if time.time() > observer_at:
            raise RuntimeError("Insufficient startup lead; increase --delay-sec")
        wait_until(observer_at)
        runtime = None
        if observer == "rp_run":
            if subprocess.run([rp_bin, "topic", "list"], env=ros_env, capture_output=True, timeout=3).returncode == 0:
                raise RuntimeError("An rp runtime is already active")
            runtime = start(["bash", "-c", "exec " + rp_command], "observer.log", True)
            deadline = min(capture_at - observer_lead_sec, time.time() + 10)
            while time.time() < deadline:
                if runtime.poll() is not None:
                    raise RuntimeError("rp runtime failed; inspect observer.log")
                if subprocess.run([rp_bin, "topic", "list"], env=ros_env, capture_output=True, timeout=1).returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("rp runtime was not ready before observer stabilization")
        elif observer == "ros2_daemon":
            with (folder / "observer.log").open("w") as log:
                subprocess.run(["ros2", "daemon", "start"], env=ros_env, check=True,
                               stdout=log, stderr=subprocess.STDOUT, timeout=10)
        observer_ready = time.time()
        if observer_ready + observer_lead_sec > capture_at:
            raise RuntimeError("Observer did not stabilize for 10 seconds before capture")
        wait_until(capture_at)
        base_port = 7400 + 250 * int(ROS_DOMAIN_ID)
        capture = start(["tshark", "-i", capture_interface, "-s", "262144",
                         "-f", f"udp portrange {base_port}-{base_port + 200}",
                         "-w", str(output)], "capture.log")
        wait_until(target_time - 3)
        if capture.poll() is not None or not output.exists():
            raise RuntimeError("Packet capture failed to initialize; inspect capture.log")
        if time.time() >= target_time:
            raise RuntimeError("Missed synchronized participant startup")
        topic_arg = "2" if topic_random else ("1" if single_topic else "0")
        workers = [start([binary, str(n_e), str(target_time), str(run_duration_sec),
                          topic_arg, str(i), str(total_processes)], f"participant_{i}.log")
                   for i in range(n_p)]
        for worker in workers:
            code = worker.wait(timeout=max(1, target_time + run_duration_sec + 10 - time.time()))
            if code:
                raise RuntimeError(f"Discovery participant exited {code}")
        if runtime is not None and runtime.poll() is not None:
            raise RuntimeError("rp runtime died during capture")
        if capture.poll() is not None:
            raise RuntimeError("Packet capture exited during measurement")
        stop(capture)
        if capture.returncode not in (0, -signal.SIGINT):
            raise RuntimeError(f"Packet capture failed: {capture.returncode}")
        reports = list(Path(report_dir).glob("*_topic_endpoint_count.csv"))
        if len(reports) != n_p or any(len(list(csv.DictReader(f.open()))) != 1 for f in reports):
            raise RuntimeError("Not all participants completed endpoint matching")
        for report in Path(report_dir).iterdir():
            shutil.copy2(report, folder / report.name)
        completion = max(local_completion(folder, n_p, total_processes,
                                          target_time, target_time + run_duration_sec))
        fields = ["frame.number", "frame.time_epoch", "ip.src", "ip.dst", "udp.srcport", "udp.dstport",
                  "rtps.sm.id", "rtps.sm.wrEntityId", "rtps.sm.rdEntityId", "rtps.guidPrefix.src"]
        csv_path = output.with_name(f"discovery_capture_N_P_{n_p}_N_E_{n_e}.csv")
        command = ["tshark", "-r", str(output), "-Y", "rtps", "-T", "fields"]
        for field in fields:
            command += ["-e", field]
        command += ["-E", "header=y", "-E", "separator=\t", "-E", "occurrence=a"]
        with csv_path.open("w") as stream:
            subprocess.run(command, stdout=stream, check=True)
        if not list(csv.DictReader(csv_path.open(), delimiter="\t")):
            raise RuntimeError("No RTPS packets captured")
        (folder / "run_metadata.json").write_text(json.dumps({
            "protocol": PROTOCOL, "host": socket.gethostname(), "N_P": n_p, "N_E": n_e,
            "total_processes": total_processes, "ros_domain_id": int(ROS_DOMAIN_ID),
            "condition": observer, "interface": capture_interface,
            "observer_ready": observer_ready, "capture_start": capture_at,
            "measure_start": target_time, "capture_deadline": target_time + run_duration_sec,
            "local_discovery_complete": str(completion),
            "measurement_end_basis": "master combines matching reports from both slaves",
            "matched_processes": len(reports), "counting_unit": "RTPS submessage",
        }, indent=2) + "\n")
        import importlib.util
        spec = importlib.util.spec_from_file_location("discovery_counts", Path(__file__).resolve().parents[1] / "analysis/analyze.py")
        analysis = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analysis)
        # Local sanity check only. Master uses the latest match across BOTH slaves.
        counts = analysis._count_run(str(csv_path), completion, target_time)
        if counts["discovery_packets"] == 0:
            raise RuntimeError("No classified discovery messages inside measurement interval")
    finally:
        for proc, privileged in reversed(owned):
            stop(proc, privileged)
        if observer == "ros2_daemon":
            subprocess.run(["ros2", "daemon", "stop"], env=ros_env, timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for log in logs:
            log.close()
        shutil.rmtree(report_dir)
    status.write_text("complete\n")
    print(f"[slave] Complete: {folder}")
    return 0


def main() -> int:

    parser = argparse.ArgumentParser(
        description="슬레이브: 목표 시각 5초 전 캡처 시작, 목표 시각에 N_P개 discovery_test 동시 실행"
    )
    parser.add_argument("--target-time", type=int, required=True,
                        help="목표 시작 시각 (Unix timestamp)")
    parser.add_argument("--N_E", type=int, choices=[2], required=True,
                        help="엔드포인트 개수")
    parser.add_argument("--N_P", type=int, choices=[1, 3, 5], required=True,
                        help="프로세스 개수 (동시에 실행할 discovery_test 개수)")
    parser.add_argument("--total-processes", type=int, default=None,
                        help="실험 전체 discovery_test 개수 (기본: N_P와 동일)")
    parser.add_argument("--run-duration", type=int, default=7,
                        help="discovery_test 실행 유지 시간(초)")
    parser.add_argument("--single-topic", action="store_true",
                        help="한 토픽에 몰아넣기 (stress_topic_)")
    parser.add_argument("--topic-random", action="store_true",
                        help="토픽을 stress_topic_0..N_E 중 랜덤 선택 (single-topic과 동시 지정 시 random 우선)")
    parser.add_argument("--capture-output", type=str, default="results/01_discovery/{host}/manual/discovery_capture.pcap",
                        help="저장할 pcap 파일 경로 (상대 경로면 패키지 디렉터리 기준)")
    parser.add_argument("--capture-interface", type=str, default="eno1",
                        help="캡처 인터페이스")
    parser.add_argument("--observer", choices=["baseline", "rp_run", "ros2_daemon"], default="baseline",
                        help="Experiment 1 observer condition")
    parser.add_argument("--observer-lead-sec", type=int, default=10,
                        help="target_time보다 observer를 먼저 시작할 시간(초)")
    parser.add_argument("--rp-command", type=str, default="sudo -n rp run",
                        help="rp_run observer condition에서 실행할 명령")
    parser.add_argument("--ros-setup", type=str, default="/opt/ros/humble/setup.bash",
                        help="ROS setup 경로")
    parser.add_argument("--workspace-setup", type=str, default="~/ros2probe-exp/install/setup.bash",
                        help="워크스페이스 setup 경로")
    parser.add_argument("--fastdds-lib-dir", type=str, default="", help="실험")

    args = parser.parse_args()  # 명령줄에서 위 인자들 파싱

    return run_slave(
        target_time=args.target_time,
        n_e=args.N_E,
        n_p=args.N_P,
        total_processes=(args.total_processes if args.total_processes is not None else args.N_P),
        run_duration_sec=args.run_duration,
        single_topic=args.single_topic,
        topic_random=args.topic_random,
        capture_output=args.capture_output,
        capture_interface=args.capture_interface,
        ros_setup=os.path.expanduser(args.ros_setup),
        workspace_setup=os.path.expanduser(args.workspace_setup),  # ~ 를 홈 디렉터리 경로로 치환
        fastdds_lib_dir=os.path.expanduser(args.fastdds_lib_dir),
        observer=args.observer,
        observer_lead_sec=args.observer_lead_sec,
        rp_command=args.rp_command,
    )


if __name__ == "__main__":  # 이 파일을 직접 실행할 때만 main 호출
    sys.exit(main())  # main 반환값을 프로세스 종료 코드로 사용
