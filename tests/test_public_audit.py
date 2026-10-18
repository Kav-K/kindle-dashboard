"""Publication gate uses inert temporary repositories, never a credential vault."""
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('publication_audit',ROOT/'tools/public_audit.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)

class PublicationAudit(unittest.TestCase):
    def test_examples_and_references_are_not_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.example.json').write_text('{"api_key_secret":"plex-key"}')
            self.assertEqual(audit.audit(root),(1,[]))

    def test_token_reports_only_rule_and_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'oops.txt').write_text('gh'+'p_'+'A'*36)
            count,issues=audit.audit(root)
            self.assertEqual((count,issues),(1,[('oops.txt','github-token')]))
            self.assertNotIn('A'*36,str(issues))

    def test_private_key_and_credential_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'secrets.json').write_text('{}')
            (root/'key.txt').write_text('-----BEGIN '+'OPENSSH PRIVATE KEY-----')
            _,issues=audit.audit(root)
            self.assertIn(('secrets.json','private-or-generated-file'),issues)
            self.assertIn(('key.txt','private-key'),issues)

    def test_real_source_tree_is_clean(self):
        count,issues=audit.audit(ROOT)
        self.assertGreater(count,15)
        self.assertEqual(issues,[])

    def test_staged_secret_cannot_hide_behind_clean_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            subprocess.run(['git','init','--quiet',str(root)],check=True,capture_output=True)
            path=root/'file.txt'
            path.write_text('gh'+'p_'+'A'*36)
            subprocess.run(['git','-C',str(root),'add','file.txt'],check=True,capture_output=True)
            path.write_text('clean source')
            self.assertEqual(audit.audit(root,staged=True),(0,[('.', 'index-worktree-mismatch')]))
            subprocess.run(['git','-C',str(root),'add','file.txt'],check=True,capture_output=True)
            self.assertEqual(audit.audit(root,staged=True),(1,[]))

if __name__=='__main__':unittest.main()
