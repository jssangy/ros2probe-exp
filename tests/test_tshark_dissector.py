"""An offline synthetic RTPS fixture; no experiment values are manufactured."""
import csv, importlib.util, io, os, shutil, socket, struct, subprocess, tempfile, unittest
from pathlib import Path
TSHARK=os.environ.get('TSHARK_BIN') or shutil.which('tshark')

@unittest.skipUnless(TSHARK, 'optional tshark dissector is not installed')
class RTPSDissector(unittest.TestCase):
    def test_acknack_followed_by_spdp_uses_distinct_writer_ids(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)
            guid=bytes.fromhex('010f00000102030405060708')
            ack=bytes.fromhex('000003c7000003c2')+struct.pack('<IIII',0,1,0,1)
            params=bytes.fromhex('00030000')+struct.pack('<HH',0x50,16)+guid+bytes.fromhex('000001c1')+struct.pack('<HH',1,0)
            data=struct.pack('<HH',0,16)+bytes.fromhex('00000000000100c2')+struct.pack('<II',0,1)+params
            rtps=b'RTPS'+bytes([2,3,1,15])+guid+bytes([6,1])+struct.pack('<H',len(ack))+ack+bytes([0x15,5])+struct.pack('<H',len(data))+data
            udp=struct.pack('!HHHH',7410,7410,8+len(rtps),0)+rtps
            ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),1,0,64,17,0,socket.inet_aton('192.0.2.1'),socket.inet_aton('192.0.2.2'))
            words=struct.unpack('!10H',ip);checksum=sum(words);checksum=(checksum&0xffff)+(checksum>>16);checksum=(checksum&0xffff)+(checksum>>16)
            ip=ip[:10]+struct.pack('!H',(~checksum)&0xffff)+ip[12:]
            frame=b'\x00'*12+b'\x08\x00'+ip+udp
            pcap=struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,1)+struct.pack('<IIII',10,500000,len(frame),len(frame))+frame
            path=out/'synthetic_discovery.pcap';path.write_bytes(pcap)
            fields=['frame.number','frame.time_epoch','ip.src','ip.dst','rtps.sm.id','rtps.sm.wrEntityId','rtps.sm.rdEntityId','rtps.guidPrefix.src']
            cmd=[TSHARK,'-r',str(path),'-Y','rtps','-T','fields','-E','header=y','-E','separator=\t']
            for field in fields:cmd+=['-e',field]
            text=subprocess.check_output(cmd,text=True)
            (out/'synthetic_discovery.csv').write_text(text)
            spec=importlib.util.spec_from_file_location('counts',root/'experiments/01_discovery/analysis/analyze.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            counts=mod._count_run(str(out/'synthetic_discovery.csv'),11,10)
            assert counts['spdp_count']==1 and counts['control_count']==1 and counts['discovery_packets']==2,counts
