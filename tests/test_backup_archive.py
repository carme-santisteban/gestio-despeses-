import base64
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from backup_archive import build_archive

class Reply(io.BytesIO):
    headers = {'Content-Type': 'application/pdf'}
    def geturl(self): return 'https://res.cloudinary.com/demo/test.pdf'

class BackupTest(unittest.TestCase):
    def test_remote_inline_failure_and_manifest(self):
        snapshot = {'exported_at':'2026-10-07', 'tables': {
            'documents_personals_adjunts':[{'id':1,'document_nom':'../../test.pdf','document_url':'https://res.cloudinary.com/demo/test.pdf'},
                {'id':2,'document_url':'https://res.cloudinary.com/demo/missing.pdf'}],
            'torn_ofici':[{'id':3,'document_nom':'inline.pdf','document_data':base64.b64encode(b'inline').decode()}]}}
        with tempfile.TemporaryDirectory() as directory:
            out=os.path.join(directory,'copy.tar.gz')
            with patch('backup_archive.urllib.request.urlopen', side_effect=[Reply(b'%PDF-test'),OSError('missing')]):
                result=build_archive(snapshot,out,directory)
            self.assertEqual(result,dict(complete=False,total=3,included=2,missing=1))
            with tarfile.open(out) as tar:
                manifest=json.load(tar.extractfile('MANIFEST_ADJUNTS.json'))
                for item in manifest['files']:
                    self.assertNotIn('..',item['path'].split('/'))
                    if item['status']=='ok':
                        import hashlib
                        data=tar.extractfile(item['path']).read()
                        self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'])
                self.assertIn('INCOMPLETA',tar.extractfile('LLEGEIX-ME_BACKUP.txt').read().decode())
    def test_all_files_present(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('backup_archive.urllib.request.urlopen', return_value=Reply(b'%PDF-ok')):
                result=build_archive({'exported_at':'today','tables':{'asseguranca_documents':[{'id':1,'document_url':'https://res.cloudinary.com/demo/test.pdf'}]}},os.path.join(directory,'copy.tar.gz'),directory)
            self.assertTrue(result['complete'])

if __name__=='__main__': unittest.main()
