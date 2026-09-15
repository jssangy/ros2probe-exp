# ros2probe experiment artifact

Artifact for *ros2probe: Non-intrusive, Kernel-selective Observability for Robot
Operating System 2 Middleware*. Requested badges: **Artifacts Available** and
**Artifacts Functional**.

One master controls two slaves over SSH, synchronizes their clocks and collects
their measurements. Run the launchers from the master after setup.

In the paper's Experiment 2, **slave A is a fixed publisher laptop**, while
**slave B is a PC laptop, Jetson Orin NX or Raspberry Pi 4B**, tested separately
as the receiver.

| Experiment | Default conditions | Slave roles |
| --- | --- | --- |
| [1. Discovery](experiments/01_discovery/README.md) | NP=1,3,5; baseline, rp_run, ros2_daemon | Both: workload, observer and capture |
| [2. Resource overhead](experiments/02_resource_overhead/README.md) | ST100,ST500,ST1000; five observer conditions | A: publisher; B: subscriber, observer and samplers |
| [3. Observation fidelity](experiments/03_observation_fidelity/README.md) | Loss=0,10,20%; rp_bag and rosbag2 | A: publisher and loss injection; B: subscriber and recorder |
| [4. Probe effect](experiments/04_probe_effect/README.md) | S1–S7; BEST_EFFORT and RELIABLE; five observer conditions per link | A: publishers; B: subscribers and observer |

Every condition uses ten repetitions. Each Exp4 launcher runs both QoS profiles
on its named link. [Results](docs/RESULTS.md) explains the final CSV.

## Requirements

- Master: Ubuntu, Git, Python 3 and a sudo-capable account.
- Slaves: two native Ubuntu laptops with sudo-capable accounts, Ethernet and
  Wi-Fi. Use Ubuntu 24.04/Jazzy or Ubuntu 22.04/Humble, with the same ROS distro
  on both. Installation includes ROS and Rust.
- Network: a 1 Gbps Ethernet experiment network and an experiment Wi-Fi router.
  The master must reach both slaves on either link. Allow SSH, slave-to-slave
  DDS UDP and TCP 55001/55002, 55101/55102, 56001/56002. The master serves NTP
  on UDP 123 and needs a working upstream time source.
- **Internet on all three machines during installation.** Internal Ethernet can
  carry SSH while Wi-Fi provides Internet.
- Storage planning: 16 GiB RAM per slave, 40 GiB free disk on A and 10 GiB on the
  master. For B, allow **2 TB free recording space** for the full sequence below:
  the offered payload alone is approximately 1 TB across all recording trials,
  before metadata and extra recording time. Actual bag size depends on delivery
  and compression. Bags remain on B by default.

## First-time setup

On **each slave**, enable SSH locally:

```bash
sudo apt-get update
sudo apt-get install -y openssh-server python3
sudo systemctl enable --now ssh
whoami
ip -br -4 addr
```

Connect both Ethernet ports to the experiment router/switch. On each slave, open
**Settings → Network → Wired (gear icon) → IPv4**. Select **Automatic (DHCP)**
if the router provides DHCP, or **Manual** and enter an unused address and subnet
mask for that LAN. Apply, reconnect, and run `ip -br -4 addr` again. Keep Internet
Wi-Fi connected during installation.

On the **master**, use the archive/revision linked in the artifact submission.
For the current GitHub source:

```bash
git clone --depth 1 https://github.com/jssangy/ros2probe-exp.git
cd ros2probe-exp
```

From the repository root, including when using an extracted archive:

```bash
cp -n testbed.example.json .testbed.local.json
chmod 600 .testbed.local.json
ip -br -4 addr
```

**Edit `.testbed.local.json` before installation.** Enter addresses without `/24`
and use repository paths without spaces.

| Field | Value |
| --- | --- |
| `master.clock_address` | Master IP reachable from both slaves on both experiment networks |
| `master.sudo_password` | Master setup password; empty prompts interactively |
| `ros_distro` | `jazzy` for Ubuntu 24.04 or `humble` for Ubuntu 22.04 |
| `domain_id` | Dedicated ROS domain; default 77 |
| Each slave's `user`, `password` | Linux account and SSH/sudo password |
| Each slave's `repo` | Remote repository path, normally `~/ros2probe-exp` |
| Each slave's `wired.ip`, `wired.nic` | Ethernet IP and interface |
| Each slave's `wireless.ip`, `wireless.nic` | Experiment Wi-Fi IP and interface |

If SSH and sudo passwords differ, add `ssh_password` and `sudo_password` to the
slave entry. Keep `default_link` as `wired`. Wi-Fi entries describe the experiment
router, which may differ from the temporary Internet network.

On the master:

```bash
./install_dependencies.sh
```

This prepares SSH, packages, experiment sudo rules and chrony, deploys source,
and builds rp and all four workloads on both slaves. Installation uses wired SSH
and keeps Internet Wi-Fi active. The bundled rp is installed with Cargo into
`.tools/bin/rp`, using a **2 MiB AF_PACKET ring per interface**. Allow tens of
minutes for the first Cargo build. Setup logs: `results/setup-<timestamp>/_master/`.
Socket buffer settings are preserved. Experiments can set CPU performance mode
and raise minimum frequency; those CPU settings persist after completion.

## Run the experiments

The full experiment sequence takes **more than 36 hours**, excluding installation;
actual duration depends on the machines, network and recording storage.

Run one launcher at a time, from the repository root on the master:

```bash
./run_exp1.sh
./run_exp2.sh
./run_exp3.sh
./run_exp4_wired.sh
```

These launchers use **wired IPs for SSH and data**. The master checks the testbed,
synchronizes clocks, prepares observers/subscribers and schedules publication.
After measurement it restores interfaces, collects data and writes the experiment's CSV.
Each invocation gets a new run ID. Ctrl-C requests cleanup.

### Switch to experiment Wi-Fi

With no experiment running:

1. On each slave, open **Settings → Wi-Fi**, select the experiment router's SSID
   and enter its Wi-Fi password.
2. Run `ip -br -4 addr`. Update `wireless.ip` and `wireless.nic` in the master's
   configuration if needed.
3. Ensure the master can reach both Wi-Fi IPs, the slaves can reach each other
   and `master.clock_address`, and the router permits communication between clients.
4. On the master, run:

```bash
./run_exp4_wireless.sh
```

Wireless SSH is verified before Ethernet is disabled. Each experiment disables
the opposite slave interface and restores it afterward.

## Results

Each completed experiment produces one CSV directly in the master's `results/`:

```text
results/
  experiment1.csv
  experiment2.csv
  experiment3.csv
  experiment4-wire.csv
  experiment4-wireless.csv
```

Each file combines all conditions and repetitions for that experiment, with one
row per condition and its mean/sample SD. A successful rerun replaces only that
experiment's CSV. See [CSV columns](docs/RESULTS.md).
The `run_id` column identifies the source measurements in `results/<run-id>/`;
original bags remain on slave B under `bags/<run-id>/`.

## Contents and paper environment

`experiments/` contains workloads/controllers; `common/` contains measurement
helpers; `scripts/` handles setup, orchestration and CSV export; `tests/` contains
regression checks; `vendor/ros2probe/` contains rp source. Its
[source manifest](vendor/ros2probe-source.json) records the revision and hashes.
The ring block count is changed from 128 to 8, retaining 256 KiB blocks.

The paper used Ubuntu 22.04, kernel 5.15, ROS 2 Humble, Fast DDS 2.6.11 and these
receiver platforms:

| Platform | CPU / SoC | Cores | Listed clock | RAM |
| --- | --- | --- | --- | --- |
| Laptop | AMD Ryzen 5 7535HS | 6C / 12T | 4.6 GHz | 16 GB |
| Jetson Orin NX | Arm Cortex-A78AE | 8C | 2.0 GHz | 16 GB |
| Raspberry Pi 4B | BCM2711 / Cortex-A72 | 4C | 1.5 GHz | 8 GB |

This profile executes the laptop workload matrix with Fast DDS. Embedded receiver
comparisons require their hardware; the separate CycloneDDS check is outside this
profile. Current installation does not pin the paper's DDS version. Functional
checks used configured Ubuntu 24.04/Jazzy laptops; a full fresh-OS setup/run was
not tested. Hardware, middleware, storage and Wi-Fi affect numerical results.

Harness: [Apache 2.0](LICENSE). Bundled rp: [Apache 2.0](vendor/ros2probe/LICENSE)
and [GPL 2.0](vendor/ros2probe/LICENSE-GPL2), as specified in the upstream crates.
