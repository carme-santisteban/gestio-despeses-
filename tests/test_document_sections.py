"""Isolated API regression checks; never contacts the production database or uploads."""
import io
import os
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    os.environ['DATABASE_URL'] = 'sqlite:///' + tmp + '/test.db'
    os.environ['SECRET_KEY'] = 'test-only'
    os.environ['REMINDERS_ENABLED'] = 'false'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Production startup uses PostgreSQL DDL; build the same models in isolated SQLite.
    import ast
    import types
    source = Path('app.py').read_text()
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, ast.With)]
    m = types.ModuleType('app_under_test')
    m.__file__ = str(Path('app.py').resolve())
    sys.modules[m.__name__] = m
    exec(compile(tree, m.__file__, 'exec'), m.__dict__)
    with m.app.app_context():
        m.db.create_all()
    m.pujar_arxiu_cloudinary = lambda *args: {'secure_url': 'https://example.invalid/test.pdf'}
    client = m.app.test_client()
    with client.session_transaction() as session:
        session['auth'] = True
    assert client.get('/api/documents-personals?tipus=personal').status_code == 401
    with client.session_transaction() as session:
        session['documents_personals_ok'] = True
    def create(kind):
        r = client.post('/api/documents-personals', data={
            'nom': 'Test ' + kind, 'tipus': kind, 'categoria': 'Salut',
            'notes': 'Keep me', 'data_document': '2026-09-14',
            'documents': [(io.BytesIO(b'one'), 'one.pdf'), (io.BytesIO(b'two'), 'two.pdf')],
        })
        assert r.status_code == 201, r.get_data(as_text=True)
        return r.get_json()
    personal = create('personal')
    incident = create('incidencia')
    def listed(kind):
        r=client.get('/api/documents-personals?tipus=' + kind)
        assert r.status_code == 200
        return r.get_json()
    assert [r['id'] for r in listed('personal')] == [personal['id']]
    assert [r['id'] for r in listed('incidencia')] == [incident['id']]
    assert client.get('/api/documents-personals?tipus=bad').status_code == 400
    assert client.post('/api/documents-personals', data={'tipus':'bad'}).status_code == 400
    url='/api/documents-personals/' + str(incident['id'])
    assert client.put(url,data={'tipus':'bad'}).status_code == 400
    moved=client.put(url,data={'tipus':'personal','nom':incident['nom'],'notes':incident['notes'],
                              'categoria':incident['categoria'],'data_document':incident['data_document']})
    assert moved.status_code == 200, moved.get_data(as_text=True)
    moved=moved.get_json()
    assert moved['fitxers'] == incident['fitxers']
    assert moved['notes'] == incident['notes']
    assert moved['categoria'] == 'Salut'
    assert len(listed('personal')) == 2 and listed('incidencia') == []
    with m.app.app_context():
        legacy=m.DocumentPersonal(nom='Existing record')
        m.db.session.add(legacy);m.db.session.commit()
        assert legacy.tipus == 'incidencia'
    assert len(listed('incidencia')) == 1
    print('PASS: separate lists, create, move preserving attachments, validation, PIN, legacy default')
