#!/usr/bin/env python3
import argparse, csv, io, json, os, tempfile
from ament_index_python.packages import get_package_prefix
from pathlib import Path
import signal, subprocess, sys, time
repo=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description="Check measurement deadlines, fidelity gating, bag topic validation and resource process trees.")
parser.add_argument('--output-dir',type=Path)
args=parser.parse_args()
out=args.output_dir or Path(tempfile.mkdtemp(prefix='ros2probe-protocols-'))
out.mkdir(parents=True,exist_ok=True)
if any(out.iterdir()): parser.error('output directory must be empty')
print(f'Evidence: {out}',flush=True)
env=dict(os.environ, ROS_DOMAIN_ID='193', ROS_LOCALHOST_ONLY='1', RMW_IMPLEMENTATION='rmw_fastrtps_cpp', ROS_LOG_DIR=str(out/'ros-log'))
active=[]; extra_groups=[]; checks=[]
def node(pkg, exe, *args): return [str(Path(get_package_prefix(pkg))/'lib'/pkg/exe), *map(str,args)]
def start(name,cmd,extra=None):
 with (out/f'{name}.log').open('w') as log:
  p=subprocess.Popen(cmd,env=env|(extra or {}),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 active.append(p); return p
def stop(p):
 try: os.killpg(p.pid,signal.SIGINT)
 except ProcessLookupError: return
 try: p.wait(timeout=5)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL);p.wait()
def wait_log(name,pattern,timeout=12):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  if pattern in (out/f'{name}.log').read_text(): return
  time.sleep(.05)
 raise AssertionError((name,pattern,(out/f'{name}.log').read_text()))
def passed(name,**details):
 checks.append(dict(check=name,status='pass',**details));print(checks[-1],flush=True)
try:
 # S7's command channel must use the publisher's TwistStamped type in both QoS profiles.
 for suffix in ['', '_rel']:
  name='cmd-vel'+(suffix or '-be')
  sub=start(name+'-sub',node('rp_exp_probe_effect','s1_sub'+suffix),{'RP_EXP_MEASURE_SEC':'2'})
  wait_log(name+'-sub','SUB_READY')
  pub=start(name+'-pub',node('rp_exp_probe_effect','s1_pub'+suffix))
  assert sub.wait(timeout=8)==0
  text=(out/(name+'-sub.log')).read_text()
  assert 'MEASURE_START_MS' in text and 'FINAL [2s]' in text,text
  stop(pub)
  passed('S7 command publisher/subscriber exchange TwistStamped',qos='reliable' if suffix else 'best_effort')
 # A measured stream must finish even if every subsequent message disappears.
 sub=start('stall-sub',node('rp_exp_probe_effect','s2_sub'),{'RP_EXP_MEASURE_SEC':'3'})
 pub=start('stall-pub',node('rp_exp_probe_effect','s2_pub'))
 wait_log('stall-sub','MEASURE_START_MS')
 time.sleep(.6); stop(pub)
 assert sub.wait(timeout=5)==0
 text=(out/'stall-sub.log').read_text();assert 'FINAL [3s]' in text
 passed('measurement ends at deadline after publisher stops')
 # A publication gate prevents sequences escaping before BOTH DDS readers exist.
 gate=out/'go';gate.unlink(missing_ok=True)
 sub=start('gate-sub',node('rp_exp_fidelity','drop_image_sub',30,30,1024,'/drop_image',12))
 pub=start('gate-pub',node('rp_exp_fidelity','drop_image_pub',30,1024,30,'/drop_image',10,3,2,gate))
 time.sleep(1)
 assert 'PUBLISH_READY' not in (out/'gate-pub.log').read_text()
 recorder=start('gate-recorder',['ros2','bag','record','/drop_image','--storage','mcap','-o',str(out/'gate-bag')])
 wait_log('gate-pub','PUBLISH_READY',12)
 assert 'SEQ ' not in (out/'gate-sub.log').read_text()
 gate.touch()
 assert pub.wait(timeout=10)==0 and sub.wait(timeout=10)==0
 stop(recorder)
 assert 'PUBLISH_DONE expected=30' in (out/'gate-pub.log').read_text()
 assert 'recv 30 / expected 30' in (out/'gate-sub.log').read_text()
 passed('fidelity waits for two readers and explicit publication release')
 result=subprocess.run([sys.executable,str(repo/'common/validate_bag.py'),str(out/'gate-bag'),'/drop_image','/missing'],env=env,capture_output=True,text=True)
 assert result.returncode==1 and '/missing' in result.stderr
 passed('all-topic validation rejects a missing required topic')
 # Check unprivileged descendants as a model of sudo's additional child session.
 sys.path.insert(0,str(repo));from common.sample_resources import snapshot
 parent=start('sampler-parent',[sys.executable,'-c', 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(8)"],start_new_session=True); print(p.pid,flush=True); time.sleep(8)'])
 wait_log('sampler-parent','\n',3)
 child_pid=int((out/'sampler-parent.log').read_text().strip());extra_groups.append(child_pid)
 sample=snapshot({parent.pid})
 assert parent.pid in {k[0] for k in sample} and child_pid in {k[0] for k in sample},sample
 os.killpg(child_pid,signal.SIGTERM);stop(parent)
 passed('resource sampler includes descendants in another session',processes=len(sample))
finally:
 for group in extra_groups:
  try: os.killpg(group,signal.SIGTERM)
  except ProcessLookupError: pass
 for p in reversed(active): stop(p)
 (out/'report.json').write_text(json.dumps(checks,indent=2)+'\n')
