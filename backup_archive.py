"""Portable backup containing database snapshot and independently readable attachments."""
import base64
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import urllib.request
from urllib.parse import urlsplit, unquote


def safe_name(value):
    return re.sub(r'[^\w.() -]', '_', str(value or 'document'))[:180].strip('. ') or 'document'


def build_archive(snapshot, destination, source_dir, progress=lambda state: None):
    candidates = [(table, row) for table, rows in snapshot['tables'].items() for row in rows
                  if row.get('document_url') or (table in ('torn_ofici', 'torn_comunicacions') and row.get('document_data'))]
    manifest = []
    def add_bytes(tar, name, data):
        info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))
    with tarfile.open(destination, 'w:gz') as tar:
        add_bytes(tar, 'copies_dades/gestiocss.json', json.dumps(snapshot, ensure_ascii=False, indent=2).encode())
        for table, row in candidates:
            url = row.get('document_url') or ''
            name = row.get('document_nom') or unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'document'
            path = 'adjunts/{}/{}/{}'.format(safe_name(table), row['id'], safe_name(name))
            entry = dict(table=table, id=row['id'], source_url=url, path=path)
            try:
                with tempfile.TemporaryFile() as file:
                    if table in ('torn_ofici', 'torn_comunicacions') and row.get('document_data'):
                        file.write(base64.b64decode(row['document_data'], validate=True))
                    else:
                        parsed = urlsplit(url)
                        if parsed.scheme != 'https' or parsed.hostname != 'res.cloudinary.com':
                            raise ValueError('Origen del fitxer no compatible: cal copiar-lo manualment')
                        with urllib.request.urlopen(url, timeout=30) as response:
                            if urlsplit(response.geturl()).hostname != 'res.cloudinary.com':
                                raise ValueError('Redirecció inesperada')
                            if 'text/html' in response.headers.get('Content-Type', ''):
                                raise ValueError('El servidor ha retornat una pàgina en lloc del fitxer')
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk: break
                                file.write(chunk)
                    size = file.tell()
                    if not size: raise ValueError('Fitxer buit')
                    file.seek(0); digest = hashlib.sha256()
                    while True:
                        chunk = file.read(1024 * 1024)
                        if not chunk: break
                        digest.update(chunk)
                    file.seek(0); info = tarfile.TarInfo(path); info.size = size; info.mode = 0o600
                    tar.addfile(info, file)
                    entry.update(status='ok', size=size, sha256=digest.hexdigest())
            except Exception as error:
                entry.update(status='error', error=str(error))
            manifest.append(entry)
            progress(dict(done=len(manifest), total=len(candidates)))
        failures = [m for m in manifest if m['status'] == 'error']
        report = dict(complete=not failures, total=len(manifest), included=len(manifest)-len(failures), missing=len(failures), files=manifest)
        add_bytes(tar, 'MANIFEST_ADJUNTS.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
        text = ('CÒPIA GESTIÓ CSS — ' + ('COMPLETA' if not failures else 'INCOMPLETA: FALTEN ADJUNTS') + '\n'
                + 'Dades exportades: ' + snapshot['exported_at'] + '\n'
                + f"Adjunts inclosos: {report['included']} de {report['total']}.\n"
                + 'Els documents són a adjunts/, agrupats per taula i identificador.\n'
                + 'El JSON conserva les dades i les referències originals. MANIFEST_ADJUNTS.json relaciona cada registre amb el fitxer local i la seva empremta SHA-256.\n'
                + 'Aquesta còpia no es restaura automàticament amb el botó Importar antic. Conserva tot el paquet per a una recuperació assistida.\n'
                + 'Conté dades sensibles: guarda-la en un lloc segur.\n'
                + '\n'.join(f"FALTA: {m['path']}: {m['error']}" for m in failures))
        add_bytes(tar, 'LLEGEIX-ME_BACKUP.txt', text.encode())
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'venv', '.venv', 'node_modules', 'tests'}]
            for name in files:
                if name.startswith('.env') or name.endswith(('.pyc', '.db', '.tar.gz')): continue
                path = os.path.join(root, name)
                if os.path.islink(path): continue
                tar.add(path, arcname='codi_gestiocss/' + os.path.relpath(path, source_dir))
    return {k:v for k,v in report.items() if k != 'files'}
