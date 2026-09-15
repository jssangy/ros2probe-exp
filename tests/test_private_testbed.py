"""One private config must select both links without leaking installation secrets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import testbed
import ssh_access
import run_experiments as master

SECRET = 'fixture-private-password-9174'


def configuration():
    value=json.loads((ROOT/'testbed.example.json').read_text())
    value['master']['sudo_password']=SECRET+'-master'
    for slave in value['slaves'].values(): slave['password']=SECRET
    return value


def write_config(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class PrivateConfig(unittest.TestCase):
    def test_both_links_select_addresses_and_strip_all_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'private.json'; doc=configuration()
            doc['slaves']['slave_a']['unexpected']={'token':SECRET}
            write_config(path,doc)
            for link, a, b in [('wired','192.168.0.2','192.168.0.6'),('wireless','192.168.0.5','192.168.0.4')]:
                config=testbed.load(path,link)
                self.assertEqual(config['hosts']['slave_a']['address'],a)
                self.assertEqual(config['hosts']['slave_b']['address'],b)
                self.assertEqual(config['hosts']['slave_a']['ssh'],'csilab@'+a)
                self.assertNotIn(SECRET,json.dumps(config))
                self.assertNotIn('password',json.dumps(config))
                job=testbed.request(config,'slave_a','trial','job',['true'])
                self.assertNotIn(SECRET,json.dumps(job))

    def test_credentials_are_separate_and_support_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.json';doc=configuration()
            doc['slaves']['slave_b']['ssh_password']=SECRET+'-ssh'
            doc['slaves']['slave_b']['sudo_password']=SECRET+'-sudo'
            write_config(p,doc)
            secrets=testbed.credentials(p)
            self.assertEqual(secrets['master'],SECRET+'-master')
            self.assertEqual(secrets['slaves']['slave_a'],dict(ssh=SECRET,sudo=SECRET))
            self.assertEqual(secrets['slaves']['slave_b'],dict(ssh=SECRET+'-ssh',sudo=SECRET+'-sudo'))

    def test_shared_file_permissions_are_rejected_without_printing_secret(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.json';write_config(p,configuration());p.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'chmod 600') as caught: testbed.load(p)
            self.assertNotIn(SECRET,str(caught.exception))

    def test_unselected_link_is_also_validated_before_setup(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.json';doc=configuration()
            doc['slaves']['slave_b']['wireless']['ip']='192.168.0.5'
            write_config(p,doc)
            with self.assertRaises(ValueError): testbed.load(p,'wired')

    def test_dry_runs_do_not_disclose_passwords_or_modify_config(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.json';write_config(p,configuration())
            before=p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode
            commands=[['scripts/setup_testbed.py','--config',str(p),'--skip-master','--dry-run'],
                      ['scripts/run_experiments.py','--config',str(p),'--link','wireless','run','4','--run-id','private-dry-run','--dry-run']]
            for argv in commands:
                r=subprocess.run([sys.executable,*argv],cwd=ROOT,capture_output=True,text=True,timeout=10)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertNotIn(SECRET,r.stdout+r.stderr)
                json.loads(r.stdout)
            self.assertEqual(before,(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode))

    def test_custom_config_is_excluded_from_deployment(self):
        doc=configuration()
        config=testbed.public_config(doc)
        with patch.object(master.subprocess,'run') as run:
            master.deploy(config,ROOT/'custom-private.json')
        transfers=[c.args[0] for c in run.call_args_list if c.args[0][0]=='rsync']
        self.assertEqual(len(transfers),2)
        for argv in transfers:
            self.assertIn('/custom-private.json',argv)
            self.assertIn('/.testbed.*.json',argv)
            self.assertIn('/.tools',argv)

    def test_password_auth_uses_only_file_reference_in_environment(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'private.json';write_config(path,configuration())
            outcomes=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],0),subprocess.CompletedProcess([],0)]
            with patch.object(ssh_access.subprocess,'run',side_effect=outcomes) as run, \
                 patch.object(ssh_access,'ensure_key',return_value='ssh-ed25519 AAAA fixture'):
                result=ssh_access.ensure_access({'ssh':'user@192.0.2.2'},path,'slave_a',SECRET)
            self.assertEqual(result,'project-key-registered')
            auth=run.call_args_list[1]
            self.assertEqual(auth.kwargs['env']['RP_EXP_CREDENTIALS_FILE'],str(path))
            self.assertEqual(auth.kwargs['env']['SSH_ASKPASS_REQUIRE'],'force')
            self.assertEqual(auth.kwargs['stdin'],subprocess.DEVNULL)
            for call in run.call_args_list:
                self.assertNotIn(SECRET,str(call.args))
                self.assertNotIn(SECRET,str(call.kwargs.get('env',{})))

    def test_authentication_failure_does_not_copy_server_output_into_logs(self):
        outcomes=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],1,stdout=SECRET,stderr=SECRET)]
        with patch.object(ssh_access.subprocess,'run',side_effect=outcomes), \
             patch.object(ssh_access,'ensure_key',return_value='ssh-ed25519 AAAA fixture'), \
             self.assertRaises(RuntimeError) as caught:
            ssh_access.ensure_access({'ssh':'user@192.0.2.2'},'/private.json','slave_a',SECRET)
        self.assertNotIn(SECRET,str(caught.exception))

    def test_public_example_contains_no_passwords(self):
        doc=json.loads((ROOT/'testbed.example.json').read_text())
        self.assertEqual(doc['master']['sudo_password'],'')
        self.assertTrue(all(s['password']=='' for s in doc['slaves'].values()))


if __name__ == '__main__': unittest.main()
