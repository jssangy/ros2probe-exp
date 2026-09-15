#!/usr/bin/env python3
# 이 스크립트는 Python 3 인터프리터로 실행됨

"""
마스터 호스트에서 N_E(엔드포인트 개수), N_P(슬레이브당 프로세스 개수)를 입력받아
각 슬레이브에 SSH로 slave_controller.py를 실행한다.
slave_controller는 목표 시각 5초 전에 패킷 캡처를 시작하고, 목표 시각에 N_P개 discovery_test를 동시 실행한다.
목표 시작 시각(현재 시각 + delay_sec)을 계산해 인자로 넘긴다.
"""

import shlex

import argparse  # 명령줄 인자 파싱
import subprocess  # SSH 등 외부 프로세스 실행
import sys  # 표준 입출력·종료 코드
import time  # 현재 시각(Unix timestamp) 계산
from concurrent.futures import ThreadPoolExecutor, as_completed  # 여러 호스트에 병렬 SSH
from typing import Optional  # 타입 힌트용


def run_ssh(host: str, remote_cmd: str, timeout: Optional[int] = None) -> tuple:
    """한 호스트에 SSH로 remote_cmd 실행. (host, returncode, stdout, stderr) 반환."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", host, remote_cmd]  # SSH 명령 리스트 (호스트 키 자동 수락)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,  # stdout·stderr 캡처
            text=True,  # 문자열로 반환
            timeout=timeout,  # None이면 무제한
        )
        return (host, result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        return (host, -1, "", "timeout")
    except Exception as e:
        return (host, -1, "", str(e))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="마스터에서 N_E, N_P를 입력받아 슬레이브들에 discovery_test를 동시 실행"
    )
    parser.add_argument(
        "N_E",
        type=int,
        choices=[2],
        help="엔드포인트 개수 (discovery_test 첫 번째 인자)",
    )
    parser.add_argument(
        "N_P",
        type=int,
        choices=[1, 3, 5],
        help="슬레이브당 프로세스 개수 (각 슬레이브에서 ros2 run을 실행하는 횟수)",
    )
    parser.add_argument(
        "--hosts",
        nargs="+",
        required=True,
        help="SSH targets (user@host), explicitly supplied for this experiment",
    )
    parser.add_argument(
        "--delay-sec",
        type=int,
        default=30,
        help="지금으로부터 목표 시작 시각(디스커버리 시작)까지의 초. observer는 기본 10초 전, 캡처는 5초 전 시작",
    )
    parser.add_argument(
        "--run-duration-sec",
        type=int,
        default=7,
        help="Executor run duration in seconds (default: 7)",
    )
    parser.add_argument(
        "--ros-setup",
        type=str,
        default="/opt/ros/humble/setup.bash",
        help="원격에서 source할 ROS setup 경로",
    )
    parser.add_argument(
        "--workspace-setup",
        type=str,
        default="~/ros2probe-exp/install/setup.bash",
        help="원격에서 source할 워크스페이스 setup 경로",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="호스트당 SSH 명령 타임아웃(초). 기본은 무제한 (실험이 끝날 때까지 대기)",
    )
    parser.add_argument(
        "--slave-script-path",
        type=str,
        default="~/ros2probe-exp/experiments/01_discovery/scripts/slave_controller.py",
        help="슬레이브에서 실행할 slave_controller.py 경로 (원격 경로)",
    )
    parser.add_argument(
        "--single-topic",
        action="store_true",
        help="한 토픽에 몰아넣기 (stress_topic_)",
    )
    parser.add_argument(
        "--topic-random",
        action="store_true",
        help="토픽을 stress_topic_0..N_E 중 랜덤 선택",
    )
    parser.add_argument(
        "--capture-output",
        type=str,
        default="results/01_discovery/{host}/manual/discovery_capture.pcap",
        help="슬레이브에서 저장할 pcap 파일 경로",
    )
    parser.add_argument(
        "--capture-interface",
        type=str,
        required=True,
        help="슬레이브에서 사용할 capture interface",
    )
    parser.add_argument(
        "--observer",
        choices=["baseline", "rp_run", "ros2_daemon"],
        default="baseline",
        help="Experiment 1 observer condition",
    )
    parser.add_argument(
        "--observer-lead-sec",
        type=int,
        default=10,
        help="Observer stabilization time before capture (minimum 10 seconds)",
    )

    #-----------------------추가한 부분----------------------------------
    parser.add_argument("--fastdds-lib-dir", type=str, default="", help="실험",)
    #-----------------------추가한 부분----------------------------------
    parser.add_argument("--dry-run", action="store_true", help="Print commands without SSH or experiments")
    args = parser.parse_args()  # 명령줄에서 위 인자들 파싱

    # 목표 시작 시각 = 현재 시각 + delay_sec (Unix timestamp)
    target_timestamp = int(time.time()) + args.delay_sec
    print(f"[master] N_E={args.N_E}, N_P={args.N_P}, hosts={args.hosts}")  # 입력 파라미터 출력
    print(f"[master] Target start time (Unix): {target_timestamp} (now + {args.delay_sec}s)")  # 목표 시각 출력
    print(f"[master] Run duration: {args.run_duration_sec}s")  # 실행 유지 시간 출력
    host_count = len(args.hosts)
    total_processes = args.N_P if host_count == 1 else args.N_P * host_count
    scope = "intra" if host_count == 1 else "inter"
    print(f"[master] Scope: {scope}, total_processes={total_processes}")

    # 각 슬레이브에서 slave_controller.py 실행 (캡처 5초 전 시작, 목표 시각에 N_P개 discovery_test 동시 실행)
    slave_script = args.slave_script_path
    if slave_script.startswith("~/"):
        script_arg = '"$HOME"/' + shlex.quote(slave_script[2:])
    else:
        script_arg = shlex.quote(slave_script)
    remote_args = [
        "--target-time", str(target_timestamp), "--N_E", str(args.N_E),
        "--N_P", str(args.N_P), "--total-processes", str(total_processes),
        "--run-duration", str(args.run_duration_sec), "--capture-output", args.capture_output,
        "--capture-interface", args.capture_interface, "--observer", args.observer,
        "--observer-lead-sec", str(args.observer_lead_sec), "--ros-setup", args.ros_setup,
        "--workspace-setup", args.workspace_setup,
    ]
    if args.topic_random:
        remote_args.append("--topic-random")
    elif args.single_topic:
        remote_args.append("--single-topic")
    if args.fastdds_lib_dir:
        remote_args.extend(["--fastdds-lib-dir", args.fastdds_lib_dir])
    remote_cmd = "python3 " + script_arg + " " + shlex.join(remote_args)
    if args.dry_run:
        print(remote_cmd)
        return 0

    failed = False
    print("[master] Sending SSH commands to all hosts (slave_controller)...")
    with ThreadPoolExecutor(max_workers=len(args.hosts)) as executor:  # 호스트 수만큼 스레드 풀 생성
        futures = {
            executor.submit(run_ssh, host, remote_cmd, args.timeout): host
            for host in args.hosts
        }  # 각 호스트에 대해 run_ssh 비동기 제출
        for future in as_completed(futures):  # 완료된 작업부터 순서 없이 처리
            host = futures[future]
            try:
                h, code, out, err = future.result()  # (호스트명, 종료코드, stdout, stderr)
                if code != 0:
                    failed = True
                    print(f"[master] {h} exit code {code}", file=sys.stderr)
                    if err:
                        print(err, file=sys.stderr)
                else:
                    print(f"[master] {h} finished.")
                if out:
                    print(out, end="")
            except Exception as e:
                failed = True
                print(f"[master] {host} error: {e}", file=sys.stderr)

    print("[master] Done.")
    return 1 if failed else 0


if __name__ == "__main__":  # 이 파일이 직접 실행될 때만 main 호출
    sys.exit(main())  # main 반환값을 프로세스 종료 코드로 사용
