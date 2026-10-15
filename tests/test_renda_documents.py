"""Exercise the real routes with SQLite, omitting PostgreSQL startup migrations."""
import ast
import base64
import io
import os
from pathlib import Path
import types
import unittest

os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['REMINDERS_ENABLED'] = 'false'
ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'app.py').read_text())
tree.body = [node for node in tree.body if not isinstance(node, ast.With)]
module = types.ModuleType('renda_test_app')
module.__file__ = str(ROOT / 'app.py')
exec(compile(tree, str(ROOT / 'app.py'), 'exec'), module.__dict__)

class RendaDocumentsTest(unittest.TestCase):
    def setUp(self):
        module.app.config['TESTING'] = True
        self.client = module.app.test_client()
        with module.app.app_context():
            module.db.create_all()
            module.RendaDocument.query.delete()
            module.db.session.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session['auth'] = True

    def upload(self, year='2025', filename='declaracio.pdf', content=b'%PDF-test'):
        return self.client.post('/api/renda/documents', data={
            'exercici': year, 'nom': 'Declaració presentada',
            'tipus': 'Declaració presentada', 'notes': 'Presentada el 30/05/2026',
            'document': (io.BytesIO(content), filename)})

    def test_private_upload_year_filter_download_and_backup(self):
        self.assertEqual(self.upload().status_code, 401)
        self.login()
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        doc = response.json
        self.assertNotIn('document_data', doc)
        self.assertEqual(len(self.client.get('/api/renda/documents?exercici=2025').json), 1)
        self.assertEqual(self.client.get('/api/renda/documents?exercici=2024').json, [])
        download = self.client.get(doc['descarregar_url'])
        self.assertEqual(download.data, b'%PDF-test')
        self.assertIn('attachment;', download.headers['Content-Disposition'])
        self.assertIn('no-store', download.headers['Cache-Control'])
        with module.app.app_context():
            backup = module.backup_complet_dades()
            self.assertEqual(base64.b64decode(backup['tables']['renda_documents'][0]['document_data']), b'%PDF-test')
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get(doc['veure_url']).status_code, 302)

    def test_reject_invalid_uploads(self):
        self.login()
        for year, filename, content in [('bad','renda.pdf',b'x'),('2025','renda.html',b'x'),('2025','renda.pdf',b''),('2025','renda.pdf',b'x'*(15*1024*1024+1))]:
            self.assertEqual(self.upload(year, filename, content).status_code, 400)
        self.assertEqual(self.client.get('/api/renda/documents?exercici=2025').json, [])

if __name__ == '__main__':
    unittest.main()
