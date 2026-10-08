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
    # Upload order must ignore the date embedded in a filename and document date.
    from datetime import datetime
    with m.app.app_context():
        record = m.db.session.get(m.DocumentPersonal, personal['id'])
        record.creat_el = datetime(2026, 10, 8, 9)
        record.document_nom = 'Future 2030-01-01.pdf'
        record.adjunts[0].creat_el = datetime(2026, 10, 8, 10)
        record.adjunts[0].document_nom = 'Old 08-10-2025.pdf'
        m.db.session.add(m.DocumentPersonalAdjunt(
            document_personal_id=record.id, document_url='https://example.invalid/latest.pdf',
            document_nom='Latest.pdf', creat_el=datetime(2026, 10, 8, 11)))
        m.db.session.commit()
    files = next(r for r in listed('personal') if r['id'] == personal['id'])['fitxers']
    assert [f['document_nom'] for f in files] == ['Latest.pdf', 'Old 08-10-2025.pdf', 'Future 2030-01-01.pdf']
    # Same timestamps have a stable order; missing timestamps go last.
    assert [d['id'] for d in m.ordenar_adjunts([
        {'id': None}, {'id': 1, 'creat_el': '2026-10-08T10:00:00'},
        {'id': 2, 'creat_el': '2026-10-08T10:00:00'}])] == [2, 1, None]
    with m.app.app_context():
        bank = m.BancConfig(nom='Test'); m.db.session.add(bank); m.db.session.flush()
        for hour, docdate in [(9, '2030-01-01'), (11, '2020-01-01'), (10, '2025-01-01')]:
            m.db.session.add(m.BancDocument(banc_id=bank.id, document_url='https://example.invalid/bank.pdf',
                document_nom=str(hour), document_data=docdate, creat_el=datetime(2026, 10, 8, hour)))
        m.db.session.commit()
        bank_id = bank.id
    assert [d['document_nom'] for d in client.get('/api/bancs/' + str(bank_id) + '/documents').get_json()] == ['11', '10', '9']
    print('PASS: inclusion order, same-day uploads, misleading filenames, bank document dates')
    print('PASS: separate lists, create, move preserving attachments, validation, PIN, legacy default')
