import os
import csv
import io
import base64
import hashlib
import mimetypes
import urllib.request
import json
import tarfile
import tempfile
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session, Response, abort, after_this_request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import extract, func
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
CALENDAR_TOKEN = os.environ.get('CALENDAR_TOKEN', 'gestiodespeses-assegurances-2026')
APP_VERSION = '2026-06-12-prestec-xavi-bancs-net'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Database
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/gestiodespeses')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

db = SQLAlchemy(app)

APP_PASSWORDS = {
    os.environ.get('APP_PASSWORD', '2026gestio'),
    os.environ.get('APP_LEGACY_PASSWORD', 'Css75917591'),
    'gestio2026',
}


def valid_app_password(value):
    return value in APP_PASSWORDS


@app.route('/api/version')
def api_version():
    return jsonify({'version': APP_VERSION})


@app.route('/api/template-version')
def api_template_version():
    template_path = os.path.join(app.root_path, 'templates', 'index.html')
    try:
        with open(template_path, encoding='utf-8') as f:
            content = f.read()
    except OSError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    return jsonify({
        'ok': True,
        'has_banc_search': 'filtrarTaulaBancs' in content,
        'has_banc_details': 'Registres dins el període' in content,
        'size': len(content),
    })


def _ics_text(value):
    value = '' if value is None else str(value)
    return value.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n').replace('\r', '')


def _ics_date(value):
    return value.strftime('%Y%m%d')

# Cloudinary config
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', '')
)

# ─── Helper Cloudinary ────────────────────────────────────────────────────────
def pujar_arxiu_cloudinary(fitxer, folder):
    """
    Puja un arxiu a Cloudinary preservant l'extensió original al public_id,
    de manera que la URL retornada inclogui l'extensió (.pdf, .jpg, etc.).
    Així el navegador desa el fitxer amb extensió correcta i s'obre amb
    l'app adequada (Vista Prèvia per a PDFs).
    """
    import re, time
    nom_original = fitxer.filename or 'document'
    nom_base, ext = os.path.splitext(nom_original)
    # Netejar el nom: només lletres, números, guió i guió baix
    nom_net = re.sub(r'[^A-Za-z0-9_-]', '_', nom_base)[:60] or 'document'
    # Sufix de temps per evitar col·lisions
    sufix = str(int(time.time() * 1000))[-8:]
    public_id_final = f"{nom_net}_{sufix}{ext.lower()}"
    return cloudinary.uploader.upload(
        fitxer,
        resource_type='raw',
        folder=folder,
        public_id=public_id_final,
        use_filename=False,
        unique_filename=False,
    )

def respond_document(filename, mimetype, data):
    filename = filename or 'document.pdf'
    ascii_name = filename.encode('ascii', 'ignore').decode('ascii') or 'document.pdf'
    return Response(
        data,
        mimetype=mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream',
        headers={'Content-Disposition': f'inline; filename="{ascii_name}"'}
    )

def prepare_cloud_document(file, folder):
    data = file.read()
    if not data:
        return None, None, None, None
    filename = file.filename or 'document.pdf'
    mimetype = file.mimetype or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    document_url = None
    try:
        stream = io.BytesIO(data)
        stream.filename = filename
        result = pujar_arxiu_cloudinary(stream, folder)
        document_url = result.get('secure_url')
    except Exception as e:
        print(f'Error Cloudinary: {e}')
    return document_url, filename, base64.b64encode(data).decode('ascii'), mimetype


def uploaded_documents():
    files = []
    seen = set()
    for field in ('documents', 'document'):
        for file in request.files.getlist(field):
            if file and file.filename and id(file) not in seen:
                files.append(file)
                seen.add(id(file))
    return files

# ─── Models ───────────────────────────────────────────────────────────────────

class Despesa(db.Model):
    __tablename__ = 'despeses'

    id            = db.Column(db.Integer, primary_key=True)
    data          = db.Column(db.Date, nullable=False, default=date.today)
    descripcio    = db.Column(db.String(500), nullable=False)
    categoria     = db.Column(db.String(100), nullable=False)
    subcategoria  = db.Column(db.String(100))
    tipus         = db.Column(db.String(20), nullable=False, default='professional')
    import_       = db.Column(db.Numeric(10, 2), nullable=False)
    proveidor     = db.Column(db.String(200))
    notes         = db.Column(db.Text)
    document_url  = db.Column(db.String(500))
    document_nom  = db.Column(db.String(200))
    incloure_renda = db.Column(db.Boolean, default=False)
    renda_exercici = db.Column(db.Integer)
    excloure_renda = db.Column(db.Boolean, default=False)
    creat_el      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        documents = []
        if self.document_url:
            documents.append({
                'id': 'principal',
                'despesa_id': self.id,
                'document_url': self.document_url,
                'document_nom': self.document_nom or 'Document',
                'principal': True,
            })
        for doc in DespesaDocument.query.filter_by(despesa_id=self.id).order_by(DespesaDocument.creat_el.asc()).all():
            doc_dict = doc.to_dict()
            doc_dict['principal'] = False
            documents.append(doc_dict)
        return {
            'id':           self.id,
            'data':         self.data.isoformat() if self.data else None,
            'descripcio':   self.descripcio,
            'categoria':    self.categoria,
            'subcategoria': self.subcategoria or '',
            'tipus':        self.tipus,
            'import_':      float(self.import_) if self.import_ else 0,
            'proveidor':    self.proveidor or '',
            'notes':        self.notes or '',
            'document_url': self.document_url or '',
            'document_nom': self.document_nom or '',
            'documents':     documents,
            'incloure_renda': bool(self.incloure_renda),
            'renda_exercici': self.renda_exercici,
            'excloure_renda': bool(self.excloure_renda),
        }


class Factura(db.Model):
    __tablename__ = 'factures'

    id            = db.Column(db.Integer, primary_key=True)
    numero        = db.Column(db.String(20), unique=True, nullable=False)
    data          = db.Column(db.Date, nullable=False, default=date.today)
    client_nom    = db.Column(db.String(200), nullable=False)
    client_nif    = db.Column(db.String(20))
    client_adreca = db.Column(db.String(300))
    concepte      = db.Column(db.Text, nullable=False)
    base          = db.Column(db.Numeric(10, 2), nullable=False)
    iva_pct       = db.Column(db.Numeric(5, 2), default=21)
    irpf_pct      = db.Column(db.Numeric(5, 2), default=0)
    notes         = db.Column(db.Text)
    estat         = db.Column(db.String(20), default='pendent')
    document_url  = db.Column(db.String(500))
    document_nom  = db.Column(db.String(200))
    creat_el      = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def iva(self):
        return float(self.base) * float(self.iva_pct) / 100

    @property
    def irpf(self):
        return float(self.base) * float(self.irpf_pct) / 100

    @property
    def total(self):
        return float(self.base) + self.iva - self.irpf

    def to_dict(self):
        return {
            'id':            self.id,
            'numero':        self.numero,
            'data':          self.data.isoformat() if self.data else None,
            'client_nom':    self.client_nom,
            'client_nif':    self.client_nif or '',
            'client_adreca': self.client_adreca or '',
            'concepte':      self.concepte,
            'base':          float(self.base),
            'iva_pct':       float(self.iva_pct),
            'irpf_pct':      float(self.irpf_pct),
            'iva':           round(self.iva, 2),
            'irpf':          round(self.irpf, 2),
            'total':         round(self.total, 2),
            'notes':         self.notes or '',
            'estat':         self.estat or 'pendent',
            'document_url':  self.document_url or '',
            'document_nom':  self.document_nom or '',
        }


# ─── Model Torn d'Ofici ───────────────────────────────────────────────────────

class TornOfici(db.Model):
    __tablename__ = 'torn_ofici'

    id             = db.Column(db.Integer, primary_key=True)
    descripcio     = db.Column(db.String(255), nullable=False)
    data_pagament  = db.Column(db.Date, nullable=False, default=date.today)
    import_brut    = db.Column(db.Numeric(10, 2), nullable=False)  # Total general ICAB
    percentatge_pagament = db.Column(db.Numeric(5, 2), default=100)
    irpf_pct       = db.Column(db.Numeric(5, 2), default=15)
    notes          = db.Column(db.Text)
    document_url   = db.Column(db.String(500))
    document_nom   = db.Column(db.String(200))
    document_data  = db.Column(db.Text)
    document_mimetype = db.Column(db.String(100), default='application/pdf')
    creat_el       = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def import_brut_cobrat(self):
        percentatge = 100 if self.percentatge_pagament is None else float(self.percentatge_pagament)
        return float(self.import_brut) * percentatge / 100

    @property
    def import_irpf(self):
        return self.import_brut_cobrat * float(self.irpf_pct) / 100

    @property
    def import_liquid(self):
        return self.import_brut_cobrat - self.import_irpf

    def to_dict(self):
        return {
            'id':            self.id,
            'descripcio':    self.descripcio,
            'data_pagament': self.data_pagament.isoformat() if self.data_pagament else None,
            'import_brut':   float(self.import_brut),
            'percentatge_pagament': 100 if self.percentatge_pagament is None else float(self.percentatge_pagament),
            'import_brut_cobrat': round(self.import_brut_cobrat, 2),
            'irpf_pct':      float(self.irpf_pct),
            'import_irpf':   round(self.import_irpf, 2),
            'import_liquid': round(self.import_liquid, 2),
            'notes':         self.notes or '',
            'document_url':  self.document_url or '',
            'document_nom':  self.document_nom or '',
            'document_data': bool(self.document_data),
            'document_download_url': url_for('download_torn_ofici_document', id=self.id) if (self.document_url or self.document_data) else '',
        }


class TornComunicacio(db.Model):
    __tablename__ = 'torn_comunicacions'

    id                = db.Column(db.Integer, primary_key=True)
    data              = db.Column(db.Date, nullable=False, default=date.today)
    tipus             = db.Column(db.String(50), default='email')
    tema              = db.Column(db.String(255))
    assumpte          = db.Column(db.String(255), nullable=False)
    notes             = db.Column(db.Text)
    document_url      = db.Column(db.String(500))
    document_nom      = db.Column(db.String(200))
    document_data     = db.Column(db.Text)
    document_mimetype = db.Column(db.String(100), default='application/pdf')
    creat_el          = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':            self.id,
            'data':          self.data.isoformat() if self.data else None,
            'tipus':         self.tipus or 'email',
            'tema':          self.tema or self.assumpte,
            'assumpte':      self.assumpte,
            'notes':         self.notes or '',
            'document_url':  self.document_url or '',
            'document_nom':  self.document_nom or '',
            'document_data': bool(self.document_data),
            'document_download_url': url_for('download_torn_comunicacio_document', id=self.id) if (self.document_url or self.document_data) else '',
        }


# SMI (Salari Mínim Interprofessional) anual — art. 213.4 LGSS
# Font: RD que fixa el SMI cada any. Actualitzar cada any al BOE.
SMI_ANUAL = {
    2024: 15876.00,   # RD 145/2024 — 1.134 € × 14
    2025: 16576.00,   # 1.184 € × 14
    2026: 17094.00,   # RD 126/2026 — 1.221 € × 14
}


# ─── Models Bancs ─────────────────────────────────────────────────────────────

class BancDocument(db.Model):
    __tablename__ = 'banc_documents'
    id           = db.Column(db.Integer, primary_key=True)
    banc_id      = db.Column(db.Integer, db.ForeignKey('bancs_config.id'), nullable=False)
    document_url = db.Column(db.String(500), nullable=False)
    document_nom = db.Column(db.String(200))
    document_data= db.Column(db.String(20))
    notes        = db.Column(db.Text)
    creat_el     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'banc_id': self.banc_id,
            'document_url': self.document_url,
            'document_nom': self.document_nom or '',
            'document_data': self.document_data or '',
            'notes': self.notes or '',
        }

class BancConfig(db.Model):
    """Llista de bancs configurats"""
    __tablename__ = 'bancs_config'
    id   = db.Column(db.Integer, primary_key=True)
    nom  = db.Column(db.String(50), unique=True, nullable=False)
    ordre = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {'id': self.id, 'nom': self.nom, 'ordre': self.ordre}


class DespesaDocument(db.Model):
    __tablename__ = 'despesa_documents'
    id           = db.Column(db.Integer, primary_key=True)
    despesa_id   = db.Column(db.Integer, db.ForeignKey('despeses.id'), nullable=False)
    document_url = db.Column(db.String(500), nullable=False)
    document_nom = db.Column(db.String(200))
    notes        = db.Column(db.Text)
    creat_el     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'despesa_id': self.despesa_id,
            'document_url': self.document_url,
            'document_nom': self.document_nom or '',
            'notes': self.notes or '',
        }


class FacturaDocument(db.Model):
    __tablename__ = 'factura_documents'
    id           = db.Column(db.Integer, primary_key=True)
    factura_id   = db.Column(db.Integer, db.ForeignKey('factures.id'), nullable=False)
    document_url = db.Column(db.String(500), nullable=False)
    document_nom = db.Column(db.String(200))
    notes        = db.Column(db.Text)
    creat_el     = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'factura_id': self.factura_id,
            'document_url': self.document_url,
            'document_nom': self.document_nom or '',
            'notes': self.notes or '',
        }


class DocumentPersonal(db.Model):
    __tablename__ = 'documents_personals'

    id             = db.Column(db.Integer, primary_key=True)
    nom            = db.Column(db.String(255), nullable=False)
    categoria      = db.Column(db.String(100), nullable=False, default='Administratiu')
    data_document  = db.Column(db.Date)
    entitat        = db.Column(db.String(200))
    notes          = db.Column(db.Text)
    document_url   = db.Column(db.String(500), nullable=False)
    document_nom   = db.Column(db.String(200))
    solucionat     = db.Column(db.Boolean, default=False)
    data_solucio   = db.Column(db.Date)
    creat_el       = db.Column(db.DateTime, default=datetime.utcnow)
    adjunts        = db.relationship('DocumentPersonalAdjunt', backref='document_personal', cascade='all,delete-orphan', order_by='DocumentPersonalAdjunt.creat_el')

    def to_dict(self):
        fitxers = [{
            'id': None,
            'document_url': self.document_url or '',
            'document_nom': self.document_nom or 'Document',
            'principal': True,
        }] if self.document_url else []
        fitxers.extend([a.to_dict() for a in self.adjunts])
        return {
            'id': self.id,
            'nom': self.nom,
            'categoria': self.categoria or 'Administratiu',
            'data_document': self.data_document.isoformat() if self.data_document else '',
            'entitat': self.entitat or '',
            'notes': self.notes or '',
            'document_url': self.document_url or '',
            'document_nom': self.document_nom or '',
            'fitxers': fitxers,
            'solucionat': bool(self.solucionat),
            'data_solucio': self.data_solucio.isoformat() if self.data_solucio else '',
            'creat_el': self.creat_el.isoformat() if self.creat_el else '',
        }


class DocumentPersonalAdjunt(db.Model):
    __tablename__ = 'documents_personals_adjunts'

    id                   = db.Column(db.Integer, primary_key=True)
    document_personal_id = db.Column(db.Integer, db.ForeignKey('documents_personals.id'), nullable=False)
    document_url         = db.Column(db.String(500), nullable=False)
    document_nom         = db.Column(db.String(200))
    creat_el             = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'document_url': self.document_url or '',
            'document_nom': self.document_nom or 'Document',
            'principal': False,
            'creat_el': self.creat_el.isoformat() if self.creat_el else '',
        }


class FotografiaBanc(db.Model):
    """Saldo real d'un banc en una data concreta"""
    __tablename__ = 'fotografies_banc'
    id     = db.Column(db.Integer, primary_key=True)
    data   = db.Column(db.Date, nullable=False)
    banc   = db.Column(db.String(50), nullable=False)
    saldo  = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    nota   = db.Column(db.String(200))

    def to_dict(self):
        return {
            'id':    self.id,
            'data':  self.data.isoformat() if self.data else None,
            'banc':  self.banc,
            'saldo': float(self.saldo) if self.saldo else 0,
            'nota':  self.nota or '',
        }


PRESTEC_XAVI_IMPORT_INICIAL = 18000.00


class PrestecXaviPagament(db.Model):
    """Pagaments de devolució del préstec personal de Xavi."""
    __tablename__ = 'prestec_xavi_pagaments'
    id            = db.Column(db.Integer, primary_key=True)
    data_pagament = db.Column(db.Date, nullable=False)
    concepte      = db.Column(db.String(255), nullable=False, default='Devolució préstec')
    import_rebut  = db.Column(db.Numeric(10, 2), nullable=False)
    compte        = db.Column(db.String(200))
    notes         = db.Column(db.Text)
    creat_el      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'data_pagament': self.data_pagament.isoformat() if self.data_pagament else None,
            'concepte': self.concepte or '',
            'import_rebut': float(self.import_rebut or 0),
            'compte': self.compte or '',
            'notes': self.notes or '',
        }


class ConfigApp(db.Model):
    """Configuració general de l'app (clau-valor)"""
    __tablename__ = 'config_app'
    id    = db.Column(db.Integer, primary_key=True)
    clau  = db.Column(db.String(50), unique=True, nullable=False)
    valor = db.Column(db.String(200))

# ─── Init DB ──────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    try:
        from sqlalchemy import text as _text
        db.session.execute(_text('ALTER TABLE assegurances ADD COLUMN IF NOT EXISTS data_itv DATE'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS incloure_renda BOOLEAN DEFAULT FALSE'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS renda_exercici INTEGER'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS excloure_renda BOOLEAN DEFAULT FALSE'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS subcategoria VARCHAR(100)'))
        db.session.execute(_text('ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS document_data TEXT'))
        db.session.execute(_text("ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS document_mimetype VARCHAR(100) DEFAULT 'application/pdf'"))
        db.session.execute(_text('ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS percentatge_pagament NUMERIC(5, 2) DEFAULT 100'))
        db.session.execute(_text('ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS tema VARCHAR(255)'))
        db.session.execute(_text('ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS document_data TEXT'))
        db.session.execute(_text("ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS document_mimetype VARCHAR(100) DEFAULT 'application/pdf'"))
        db.session.execute(_text('ALTER TABLE documents_personals ADD COLUMN IF NOT EXISTS solucionat BOOLEAN DEFAULT FALSE'))
        db.session.execute(_text('ALTER TABLE documents_personals ADD COLUMN IF NOT EXISTS data_solucio DATE'))
        db.session.execute(_text('''
            CREATE TABLE IF NOT EXISTS documents_personals_adjunts (
                id SERIAL PRIMARY KEY,
                document_personal_id INTEGER NOT NULL REFERENCES documents_personals(id) ON DELETE CASCADE,
                document_url VARCHAR(500) NOT NULL,
                document_nom VARCHAR(200),
                creat_el TIMESTAMP DEFAULT NOW()
            )
        '''))
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Crear taula credencials si no existeix
    from sqlalchemy import text
    db.session.execute(text('''
        CREATE TABLE IF NOT EXISTS credencials (
            id SERIAL PRIMARY KEY,
            categoria VARCHAR(100) NOT NULL,
            servei VARCHAR(200) NOT NULL,
            usuari VARCHAR(300),
            contrasenya VARCHAR(500),
            notes TEXT,
            creat_el TIMESTAMP DEFAULT NOW()
        )
    '''))
    db.session.commit()
    # Failsafe: crear taula torn_ofici si db.create_all() no ho va fer
    try:
        db.session.execute(text('''
            CREATE TABLE IF NOT EXISTS torn_ofici (
                id SERIAL PRIMARY KEY,
                descripcio VARCHAR(255) NOT NULL,
                data_pagament DATE NOT NULL,
                import_brut NUMERIC(10, 2) NOT NULL,
                percentatge_pagament NUMERIC(5, 2) DEFAULT 100,
                irpf_pct NUMERIC(5, 2) DEFAULT 15,
                notes TEXT,
                document_url VARCHAR(500),
                document_nom VARCHAR(200),
                document_data TEXT,
                document_mimetype VARCHAR(100) DEFAULT 'application/pdf',
                creat_el TIMESTAMP DEFAULT NOW()
            )
        '''))
        db.session.execute(text('ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS document_data TEXT'))
        db.session.execute(text("ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS document_mimetype VARCHAR(100) DEFAULT 'application/pdf'"))
        db.session.execute(text('ALTER TABLE torn_ofici ADD COLUMN IF NOT EXISTS percentatge_pagament NUMERIC(5, 2) DEFAULT 100'))
        db.session.execute(text('''
            CREATE TABLE IF NOT EXISTS torn_comunicacions (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                tipus VARCHAR(50) DEFAULT 'email',
                tema VARCHAR(255),
                assumpte VARCHAR(255) NOT NULL,
                notes TEXT,
                document_url VARCHAR(500),
                document_nom VARCHAR(200),
                document_data TEXT,
                document_mimetype VARCHAR(100) DEFAULT 'application/pdf',
                creat_el TIMESTAMP DEFAULT NOW()
            )
        '''))
        db.session.execute(text('ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS tema VARCHAR(255)'))
        db.session.execute(text('ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS document_data TEXT'))
        db.session.execute(text("ALTER TABLE torn_comunicacions ADD COLUMN IF NOT EXISTS document_mimetype VARCHAR(100) DEFAULT 'application/pdf'"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Inserir bancs per defecte si la taula és buida
    if not ConfigApp.query.filter_by(clau='pin_credencials').first():
        db.session.add(ConfigApp(clau='pin_credencials', valor='Cs75917591'))
        db.session.commit()
    if not ConfigApp.query.filter_by(clau='pin_documents_personals').first():
        db.session.add(ConfigApp(clau='pin_documents_personals', valor='Cs75917591'))
        db.session.commit()

    if BancConfig.query.count() == 0:
        bancs_defecte = ['TRADE', 'CAIXA GUISONA', 'SANTANDER', 'CETELEM', 'REVOLUT', 'BUNQ', 'CAIXA']
        for i, b in enumerate(bancs_defecte):
            db.session.add(BancConfig(nom=b, ordre=i))
        db.session.commit()

    if not ConfigApp.query.filter_by(clau='prestec_xavi_importat_excel').first():
        pagaments_inicials = [
            ('2026-04-02', 'Devolució préstec (1a quota)', 500, 'Bunq (compte conjunt)'),
            ('2026-04-03', 'Devolució préstec (2a quota)', 500, 'Bunq (compte conjunt)'),
            ('2026-06-07', 'Devolució préstec (3a quota)', 1000, 'Bunq (compte conjunt)'),
        ]
        if PrestecXaviPagament.query.count() == 0:
            for data_iso, concepte, import_rebut, compte in pagaments_inicials:
                db.session.add(PrestecXaviPagament(
                    data_pagament=datetime.strptime(data_iso, '%Y-%m-%d').date(),
                    concepte=concepte,
                    import_rebut=import_rebut,
                    compte=compte,
                    notes='Importat de Prestec_Xavi_Seguiment.xlsx'
                ))
        db.session.add(ConfigApp(clau='prestec_xavi_importat_excel', valor='2026-06-12'))
        db.session.commit()

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/auth', methods=['POST'])
def auth():
    if valid_app_password(request.json.get('pwd')):
        session['auth'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if valid_app_password(request.form.get('password')):
            session['auth'] = True
            next_url = request.form.get('next') or request.args.get('next') or '/'
            if not next_url.startswith('/') or next_url.startswith('//'):
                next_url = '/'
            return redirect(next_url)
        else:
            error = 'Contrasenya incorrecta'
    return render_template('login.html', error=error, next_url=request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.pop('auth', None)
    return redirect('/login')

@app.route('/')
def index():
    if not session.get('auth'):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for('login', next=next_url))
    return render_template(
        'index.html',
        assegurances_calendar_path=url_for('assegurances_calendar', token=CALENDAR_TOKEN)
    )

# --- CRUD Despeses ---

@app.route('/api/despeses', methods=['GET'])
def get_despeses():
    any_  = request.args.get('any', type=int)
    mes   = request.args.get('mes', type=int)
    tipus = request.args.get('tipus')
    cat   = request.args.get('categoria')
    subcat = request.args.get('subcategoria')

    q = Despesa.query
    if any_:
        q = q.filter(extract('year', Despesa.data) == any_)
    if mes:
        q = q.filter(extract('month', Despesa.data) == mes)
    if tipus:
        q = q.filter(Despesa.tipus == tipus)
    if cat:
        q = q.filter(Despesa.categoria == cat)
    if subcat:
        q = q.filter(Despesa.subcategoria == subcat)

    despeses = q.order_by(Despesa.data.desc()).all()
    return jsonify([d.to_dict() for d in despeses])

@app.route('/api/despeses', methods=['POST'])
def create_despesa():
    data = request.form
    fitxers = uploaded_documents()

    document_url = None
    document_nom = None
    cloudinary_error = None

    if fitxers:
        try:
            result = pujar_arxiu_cloudinary(fitxers[0], 'gestiodespeses')
            document_url = result.get('secure_url')
            document_nom = fitxers[0].filename
        except Exception as e:
            cloudinary_error = str(e)

    try:
        despesa = Despesa(
            data        = datetime.strptime(data['data'], '%Y-%m-%d').date(),
            descripcio  = data['descripcio'],
            categoria   = data['categoria'],
            subcategoria= data.get('subcategoria', ''),
            tipus       = data.get('tipus', 'professional'),
            import_     = float(data['import_']),
            proveidor   = data.get('proveidor', ''),
            notes       = data.get('notes', ''),
            document_url= document_url,
            document_nom= document_nom,
            excloure_renda = data.get('excloure_renda') == '1',
            incloure_renda = data.get('incloure_renda') == '1',
            renda_exercici = int(data.get('renda_exercici') or datetime.strptime(data['data'], '%Y-%m-%d').date().year) if data.get('incloure_renda') == '1' else None,
        )
        db.session.add(despesa)
        db.session.commit()
        docs_afegits = 1 if document_url else 0
        for fitxer_extra in fitxers[1:]:
            try:
                result_extra = pujar_arxiu_cloudinary(fitxer_extra, 'gestiodespeses/despeses')
                db.session.add(DespesaDocument(
                    despesa_id=despesa.id,
                    document_url=result_extra.get('secure_url'),
                    document_nom=fitxer_extra.filename,
                ))
                docs_afegits += 1
            except Exception as e:
                cloudinary_error = str(e)
        db.session.commit()
        result = despesa.to_dict()
        result['documents_afegits'] = docs_afegits
        if cloudinary_error:
            result['cloudinary_error'] = cloudinary_error
        return jsonify(result), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/despeses/<int:id>', methods=['PUT'])
def update_despesa(id):
    despesa = Despesa.query.get_or_404(id)
    data = request.form
    fitxers = uploaded_documents()

    cloudinary_error = None
    docs_afegits = 0
    if fitxers:
        try:
            result = pujar_arxiu_cloudinary(fitxers[0], 'gestiodespeses')
            despesa.document_url = result.get('secure_url')
            despesa.document_nom = fitxers[0].filename
            docs_afegits += 1
        except Exception as e:
            cloudinary_error = str(e)
        for fitxer_extra in fitxers[1:]:
            try:
                result_extra = pujar_arxiu_cloudinary(fitxer_extra, 'gestiodespeses/despeses')
                db.session.add(DespesaDocument(
                    despesa_id=despesa.id,
                    document_url=result_extra.get('secure_url'),
                    document_nom=fitxer_extra.filename,
                ))
                docs_afegits += 1
            except Exception as e:
                cloudinary_error = str(e)

    try:
        despesa.data       = datetime.strptime(data['data'], '%Y-%m-%d').date()
        despesa.descripcio = data['descripcio']
        despesa.categoria  = data['categoria']
        despesa.subcategoria = data.get('subcategoria', '')
        despesa.tipus      = data.get('tipus', 'professional')
        despesa.import_    = float(data['import_'])
        despesa.proveidor  = data.get('proveidor', '')
        despesa.notes      = data.get('notes', '')
        despesa.excloure_renda = data.get('excloure_renda') == '1'
        despesa.incloure_renda = data.get('incloure_renda') == '1'
        despesa.renda_exercici = int(data.get('renda_exercici') or despesa.data.year) if despesa.incloure_renda else None
        db.session.commit()
        result = despesa.to_dict()
        result['documents_afegits'] = docs_afegits
        if cloudinary_error:
            result['cloudinary_error'] = cloudinary_error
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/despeses/<int:id>', methods=['DELETE'])
def delete_despesa(id):
    despesa = Despesa.query.get_or_404(id)
    db.session.delete(despesa)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/despeses/<int:id>/treure-renda', methods=['POST'])
def treure_despesa_renda(id):
    despesa = Despesa.query.get_or_404(id)
    despesa.excloure_renda = True
    despesa.incloure_renda = False
    despesa.renda_exercici = None
    db.session.commit()
    return jsonify(despesa.to_dict())

# --- Estadístiques ---

@app.route('/api/estadistiques')
def estadistiques():
    any_ = request.args.get('any', datetime.now().year, type=int)

    mensuals = db.session.query(
        extract('month', Despesa.data).label('mes'),
        Despesa.tipus,
        func.sum(Despesa.import_).label('total')
    ).filter(
        extract('year', Despesa.data) == any_
    ).group_by('mes', Despesa.tipus).all()

    categories = db.session.query(
        Despesa.categoria,
        func.sum(Despesa.import_).label('total')
    ).filter(
        extract('year', Despesa.data) == any_
    ).group_by(Despesa.categoria).all()

    total_any = db.session.query(func.sum(Despesa.import_)).filter(
        extract('year', Despesa.data) == any_
    ).scalar() or 0

    total_prof = db.session.query(func.sum(Despesa.import_)).filter(
        extract('year', Despesa.data) == any_,
        Despesa.tipus == 'professional'
    ).scalar() or 0

    total_pers = db.session.query(func.sum(Despesa.import_)).filter(
        extract('year', Despesa.data) == any_,
        Despesa.tipus == 'personal'
    ).scalar() or 0

    return jsonify({
        'any': any_,
        'total_any': float(total_any),
        'total_professional': float(total_prof),
        'total_personal': float(total_pers),
        'mensuals': [{'mes': int(r.mes), 'tipus': r.tipus, 'total': float(r.total)} for r in mensuals],
        'categories': [{'categoria': r.categoria, 'total': float(r.total)} for r in categories],
    })

@app.route('/api/anys')
def get_anys():
    anys = db.session.query(
        extract('year', Despesa.data).label('any')
    ).distinct().order_by('any').all()
    return jsonify([int(r.any) for r in anys])

# --- Export CSV ---

@app.route('/api/export/csv')
def export_csv():
    any_  = request.args.get('any', type=int)
    mes   = request.args.get('mes', type=int)
    tipus = request.args.get('tipus')
    subcat = request.args.get('subcategoria')

    q = Despesa.query
    if any_:
        q = q.filter(extract('year', Despesa.data) == any_)
    if mes:
        q = q.filter(extract('month', Despesa.data) == mes)
    if tipus:
        q = q.filter(Despesa.tipus == tipus)
    if subcat:
        q = q.filter(Despesa.subcategoria == subcat)

    despeses = q.order_by(Despesa.data).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Data', 'Descripció', 'Categoria', 'Subapartat', 'Tipus', 'Import (€)', 'Proveïdor', 'Notes'])
    for d in despeses:
        writer.writerow([
            d.data.strftime('%d/%m/%Y'),
            d.descripcio,
            d.categoria,
            d.subcategoria or '',
            d.tipus.capitalize(),
            f"{float(d.import_):.2f}",
            d.proveidor or '',
            d.notes or '',
        ])

    output.seek(0)
    nom_fitxer = f"despeses_{any_ or 'tot'}_{mes or 'tot'}.csv"
    return send_file(
        io.BytesIO(output.read().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=nom_fitxer
    )

# --- Factures ---

def generar_numero_factura(data):
    """
    Format: NNMMAA (6 dígits)
      NN = número correlatiu dins de l'any (01, 02, ...)
      MM = mes de la factura
      AA = any (2 dígits)
    Reinicia a 01 cada any natural. No reassigna números de factures
    esborrades (els forats es mantenen) conforme art. 6 RD 1619/2012.
    """
    any_2d = data.year % 100
    mes_2d = data.month
    max_nn = 0
    factures_any = Factura.query.filter(
        extract('year', Factura.data) == data.year
    ).all()
    for f in factures_any:
        num = (f.numero or '').strip()
        if len(num) == 6 and num.isdigit():
            try:
                nn = int(num[:2])
                if nn > max_nn:
                    max_nn = nn
            except ValueError:
                pass
    proper_nn = max_nn + 1
    return f"{proper_nn:02d}{mes_2d:02d}{any_2d:02d}"

@app.route('/api/factures', methods=['GET'])
def get_factures():
    any_ = request.args.get('any', type=int)
    q = Factura.query
    if any_:
        q = q.filter(extract('year', Factura.data) == any_)
    return jsonify([f.to_dict() for f in q.order_by(Factura.data.desc()).all()])

@app.route('/api/factures', methods=['POST'])
def create_factura():
    fitxer = request.files.get('document')
    data = request.form if fitxer else request.json or request.form
    document_url = None
    document_nom = None
    if fitxer and fitxer.filename:
        try:
            result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/factures')
            document_url = result.get('secure_url')
            document_nom = fitxer.filename
        except Exception as e:
            pass
    try:
        data_factura = datetime.strptime(data['data'], '%Y-%m-%d').date()
        numero_manual = (data.get('numero') or '').strip()
        numero_final = numero_manual if numero_manual else generar_numero_factura(data_factura)
        factura = Factura(
            numero        = numero_final,
            data          = data_factura,
            client_nom    = data['client_nom'],
            client_nif    = data.get('client_nif', ''),
            client_adreca = data.get('client_adreca', ''),
            concepte      = data['concepte'],
            base          = float(data['base']),
            iva_pct       = float(data.get('iva_pct', 21)),
            irpf_pct      = float(data.get('irpf_pct', 0)),
            notes         = data.get('notes', ''),
            estat         = data.get('estat', 'pendent'),
            document_url  = document_url,
            document_nom  = document_nom,
        )
        db.session.add(factura)
        db.session.commit()
        return jsonify(factura.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/factures/<int:id>', methods=['PUT'])
def update_factura(id):
    factura = Factura.query.get_or_404(id)
    fitxer = request.files.get('document')
    data = request.form
    if fitxer and fitxer.filename:
        try:
            result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/factures')
            factura.document_url = result.get('secure_url')
            factura.document_nom = fitxer.filename
        except:
            pass
    try:
        factura.data          = datetime.strptime(data['data'], '%Y-%m-%d').date()
        numero_edit = (data.get('numero') or '').strip()
        if numero_edit:
            factura.numero = numero_edit
        factura.client_nom    = data['client_nom']
        factura.client_nif    = data.get('client_nif', '')
        factura.client_adreca = data.get('client_adreca', '')
        factura.concepte      = data['concepte']
        factura.base          = float(data['base'])
        factura.iva_pct       = float(data.get('iva_pct', 21))
        factura.irpf_pct      = float(data.get('irpf_pct', 0))
        factura.notes         = data.get('notes', '')
        factura.estat         = data.get('estat', factura.estat)
        db.session.commit()
        return jsonify(factura.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/factures/<int:id>', methods=['DELETE'])
def delete_factura(id):
    factura = Factura.query.get_or_404(id)
    db.session.delete(factura)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/factures/proper-numero')
def proper_numero_factura():
    data_str = request.args.get('data')
    try:
        if data_str:
            data_ref = datetime.strptime(data_str, '%Y-%m-%d').date()
        else:
            data_ref = datetime.now().date()
    except ValueError:
        data_ref = datetime.now().date()
    return jsonify({'numero': generar_numero_factura(data_ref)})

@app.route('/api/factures/anys')
def get_anys_factures():
    anys = db.session.query(
        extract('year', Factura.data).label('any')
    ).distinct().order_by('any').all()
    return jsonify([int(r.any) for r in anys])

# ─── Torn d'Ofici ─────────────────────────────────────────────────────────────

@app.route('/api/torn-ofici', methods=['GET'])
def get_torn_ofici():
    any_ = request.args.get('any', type=int)
    q = TornOfici.query
    if any_:
        q = q.filter(extract('year', TornOfici.data_pagament) == any_)
    return jsonify([t.to_dict() for t in q.order_by(TornOfici.data_pagament.desc()).all()])

@app.route('/api/torn-ofici', methods=['POST'])
def create_torn_ofici():
    fitxer = request.files.get('document')
    data = request.form if fitxer else (request.json or request.form)
    document_url = None
    document_nom = None
    document_data = None
    document_mimetype = None
    if fitxer and fitxer.filename:
        document_url, document_nom, document_data, document_mimetype = prepare_cloud_document(
            fitxer,
            'gestiodespeses/torn_ofici'
        )
    try:
        torn = TornOfici(
            descripcio    = data['descripcio'],
            data_pagament = datetime.strptime(data['data_pagament'], '%Y-%m-%d').date(),
            import_brut   = float(data['import_brut']),
            percentatge_pagament = float(data.get('percentatge_pagament', 100)),
            irpf_pct      = float(data.get('irpf_pct', 15)),
            notes         = data.get('notes', ''),
            document_url  = document_url,
            document_nom  = document_nom,
            document_data = document_data,
            document_mimetype = document_mimetype,
        )
        db.session.add(torn)
        db.session.commit()
        return jsonify(torn.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/torn-ofici/<int:id>', methods=['PUT'])
def update_torn_ofici(id):
    torn = TornOfici.query.get_or_404(id)
    fitxer = request.files.get('document')
    data = request.form
    if fitxer and fitxer.filename:
        document_url, document_nom, document_data, document_mimetype = prepare_cloud_document(
            fitxer,
            'gestiodespeses/torn_ofici'
        )
        torn.document_url = document_url
        torn.document_nom = document_nom
        torn.document_data = document_data
        torn.document_mimetype = document_mimetype
    try:
        torn.descripcio    = data['descripcio']
        torn.data_pagament = datetime.strptime(data['data_pagament'], '%Y-%m-%d').date()
        torn.import_brut   = float(data['import_brut'])
        torn.percentatge_pagament = float(data.get('percentatge_pagament', 100))
        torn.irpf_pct      = float(data.get('irpf_pct', 15))
        torn.notes         = data.get('notes', '')
        db.session.commit()
        return jsonify(torn.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/torn-ofici/<int:id>/document')
def download_torn_ofici_document(id):
    torn = TornOfici.query.get_or_404(id)
    filename = torn.document_nom or 'document.pdf'
    mimetype = torn.document_mimetype or mimetypes.guess_type(filename)[0] or 'application/pdf'
    if torn.document_url:
        try:
            with urllib.request.urlopen(torn.document_url, timeout=10) as r:
                data = r.read()
            if data:
                return respond_document(filename, mimetype, data)
        except Exception as e:
            print(f'Error baixant document TornOfici de Cloudinary: {e}')
    if torn.document_data:
        return respond_document(filename, mimetype, base64.b64decode(torn.document_data))
    abort(404)

@app.route('/api/torn-ofici/<int:id>', methods=['DELETE'])
def delete_torn_ofici(id):
    torn = TornOfici.query.get_or_404(id)
    db.session.delete(torn)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/torn-comunicacions', methods=['GET'])
def get_torn_comunicacions():
    any_ = request.args.get('any', type=int)
    q = TornComunicacio.query
    if any_:
        q = q.filter(extract('year', TornComunicacio.data) == any_)
    return jsonify([c.to_dict() for c in q.order_by(TornComunicacio.data.desc(), TornComunicacio.id.desc()).all()])

@app.route('/api/torn-comunicacions', methods=['POST'])
def create_torn_comunicacio():
    fitxer = request.files.get('document')
    data = request.form
    document_url = document_nom = document_data = document_mimetype = None
    if fitxer and fitxer.filename:
        document_url, document_nom, document_data, document_mimetype = prepare_cloud_document(
            fitxer,
            'gestiodespeses/torn_comunicacions'
        )
    try:
        comunicacio = TornComunicacio(
            data=datetime.strptime(data['data'], '%Y-%m-%d').date(),
            tipus=data.get('tipus', 'email'),
            tema=(data.get('tema') or data['assumpte']).strip(),
            assumpte=data['assumpte'],
            notes=data.get('notes', ''),
            document_url=document_url,
            document_nom=document_nom,
            document_data=document_data,
            document_mimetype=document_mimetype,
        )
        db.session.add(comunicacio)
        db.session.commit()
        return jsonify(comunicacio.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/torn-comunicacions/<int:id>', methods=['PUT'])
def update_torn_comunicacio(id):
    comunicacio = TornComunicacio.query.get_or_404(id)
    fitxer = request.files.get('document')
    data = request.form
    if fitxer and fitxer.filename:
        document_url, document_nom, document_data, document_mimetype = prepare_cloud_document(
            fitxer,
            'gestiodespeses/torn_comunicacions'
        )
        comunicacio.document_url = document_url
        comunicacio.document_nom = document_nom
        comunicacio.document_data = document_data
        comunicacio.document_mimetype = document_mimetype
    try:
        comunicacio.data = datetime.strptime(data['data'], '%Y-%m-%d').date()
        comunicacio.tipus = data.get('tipus', 'email')
        comunicacio.tema = (data.get('tema') or data['assumpte']).strip()
        comunicacio.assumpte = data['assumpte']
        comunicacio.notes = data.get('notes', '')
        db.session.commit()
        return jsonify(comunicacio.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/torn-comunicacions/<int:id>/document')
def download_torn_comunicacio_document(id):
    comunicacio = TornComunicacio.query.get_or_404(id)
    filename = comunicacio.document_nom or 'document.pdf'
    mimetype = comunicacio.document_mimetype or mimetypes.guess_type(filename)[0] or 'application/pdf'
    if comunicacio.document_url:
        try:
            with urllib.request.urlopen(comunicacio.document_url, timeout=10) as r:
                data = r.read()
            if data:
                return respond_document(filename, mimetype, data)
        except Exception as e:
            print(f'Error baixant document comunicacio torn de Cloudinary: {e}')
    if comunicacio.document_data:
        return respond_document(filename, mimetype, base64.b64decode(comunicacio.document_data))
    abort(404)

@app.route('/api/torn-comunicacions/<int:id>', methods=['DELETE'])
def delete_torn_comunicacio(id):
    comunicacio = TornComunicacio.query.get_or_404(id)
    db.session.delete(comunicacio)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/torn-ofici/anys')
def get_anys_torn_ofici():
    anys = db.session.query(
        extract('year', TornOfici.data_pagament).label('any')
    ).distinct().order_by('any').all()
    return jsonify([int(r.any) for r in anys])

@app.route('/api/resum-smi')
def resum_smi():
    """
    Retorna el resum d'ingressos anuals per comparar amb el SMI
    segons art. 213.4 LGSS (compatibilitat jubilació amb activitat per compte propi).
    Suma: base imposable factures emeses + import brut torn d'ofici.
    """
    any_ = request.args.get('any', type=int, default=datetime.now().year)
    # Base imposable de les factures emeses l'any indicat
    factures = Factura.query.filter(extract('year', Factura.data) == any_).all()
    total_factures = sum(float(f.base) for f in factures)
    # Import brut del torn d'ofici l'any indicat (per data de pagament)
    torns = TornOfici.query.filter(extract('year', TornOfici.data_pagament) == any_).all()
    total_torn = sum(t.import_brut_cobrat for t in torns)
    total_general = total_factures + total_torn
    smi = SMI_ANUAL.get(any_, 0)
    percent = (total_general / smi * 100) if smi else 0
    return jsonify({
        'any':            any_,
        'total_factures': round(total_factures, 2),
        'total_torn':     round(total_torn, 2),
        'total_general':  round(total_general, 2),
        'smi_anual':      smi,
        'percent_smi':    round(percent, 2),
        'restant':        round(smi - total_general, 2) if smi else None,
    })

@app.route('/api/renda')
def api_renda():
    any_ = request.args.get('any', type=int, default=date.today().year - 1)
    factures = Factura.query.filter(extract('year', Factura.data) == any_).order_by(Factura.data).all()
    torns = TornOfici.query.filter(extract('year', TornOfici.data_pagament) == any_).order_by(TornOfici.data_pagament).all()
    despeses_renda = Despesa.query.filter(
        Despesa.incloure_renda == True,
        Despesa.renda_exercici == any_,
        db.or_(Despesa.excloure_renda == False, Despesa.excloure_renda == None)
    ).order_by(Despesa.data).all()
    despeses_prof = Despesa.query.filter(
        extract('year', Despesa.data) == any_,
        Despesa.tipus == 'professional',
        db.or_(Despesa.incloure_renda == False, Despesa.incloure_renda == None),
        db.or_(Despesa.excloure_renda == False, Despesa.excloure_renda == None)
    ).order_by(Despesa.data).all()
    total_base = sum(float(f.base or 0) for f in factures)
    total_iva = sum(float(f.iva or 0) for f in factures)
    total_irpf_factures = sum(float(f.irpf or 0) for f in factures)
    total_torn_brut = sum(t.import_brut_cobrat for t in torns)
    total_torn_irpf = sum(float(t.import_irpf or 0) for t in torns)
    total_despeses_prof = sum(float(d.import_ or 0) for d in despeses_prof)
    total_despeses_marcades = sum(float(d.import_ or 0) for d in despeses_renda)
    return jsonify({
        'any': any_,
        'factures': [f.to_dict() for f in factures],
        'torn_ofici': [t.to_dict() for t in torns],
        'despeses_professionals': [d.to_dict() for d in despeses_prof],
        'despeses_marcades': [d.to_dict() for d in despeses_renda],
        'totals': {
            'factures_base': round(total_base, 2),
            'factures_iva': round(total_iva, 2),
            'factures_irpf': round(total_irpf_factures, 2),
            'torn_brut': round(total_torn_brut, 2),
            'torn_irpf': round(total_torn_irpf, 2),
            'despeses_professionals': round(total_despeses_prof, 2),
            'despeses_marcades': round(total_despeses_marcades, 2),
        }
    })

# ─── PIN Bancs ────────────────────────────────────────────────────────────────

@app.route('/api/bancs/verificar-pin', methods=['POST'])
def verificar_pin_bancs():
    data = request.get_json()
    pin_introduit = data.get('pin', '')
    config = ConfigApp.query.filter_by(clau='pin_bancs').first()
    if not config:
        # Si no hi ha PIN configurat, acceptar qualsevol
        return jsonify({'ok': True})
    if config.valor == pin_introduit:
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/bancs/pin', methods=['GET'])
def get_pin_bancs():
    config = ConfigApp.query.filter_by(clau='pin_bancs').first()
    return jsonify({'te_pin': config is not None and bool(config.valor)})

@app.route('/api/bancs/pin', methods=['POST'])
def set_pin_bancs():
    if not session.get('auth'):
        return jsonify({'error': 'No autoritzat'}), 401
    data = request.get_json()
    nou_pin = data.get('pin', '').strip()
    if not nou_pin:
        return jsonify({'error': 'Cal un PIN'}), 400
    config = ConfigApp.query.filter_by(clau='pin_bancs').first()
    if config:
        config.valor = nou_pin
    else:
        config = ConfigApp(clau='pin_bancs', valor=nou_pin)
        db.session.add(config)
    db.session.commit()
    return jsonify({'ok': True})

# ─── API Bancs ────────────────────────────────────────────────────────────────

@app.route('/api/bancs/config', methods=['GET'])
def get_bancs_config():
    bancs = BancConfig.query.order_by(BancConfig.ordre, BancConfig.id).all()
    return jsonify([b.to_dict() for b in bancs])

@app.route('/api/bancs/config', methods=['POST'])
def create_banc_config():
    data = request.get_json()
    nom = data.get('nom', '').strip()
    if not nom:
        return jsonify({'error': 'Cal un nom'}), 400
    if BancConfig.query.filter_by(nom=nom).first():
        return jsonify({'error': 'Ja existeix aquest banc'}), 400
    ordre = BancConfig.query.count()
    banc = BancConfig(nom=nom, ordre=ordre)
    db.session.add(banc)
    db.session.commit()
    return jsonify(banc.to_dict()), 201

@app.route('/api/bancs/config/<int:id>', methods=['DELETE'])
def delete_banc_config(id):
    banc = BancConfig.query.get_or_404(id)
    # Verificar que no té fotografies
    count = FotografiaBanc.query.filter_by(banc=banc.nom).count()
    if count > 0:
        return jsonify({'error': f'No es pot eliminar: el banc té {count} registres'}), 400
    db.session.delete(banc)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/bancs/<int:banc_id>/documents', methods=['GET'])
def get_banc_documents(banc_id):
    docs = BancDocument.query.filter_by(banc_id=banc_id).order_by(BancDocument.document_data.asc(), BancDocument.creat_el.asc()).all()
    return jsonify([d.to_dict() for d in docs])

@app.route('/api/bancs/<int:banc_id>/documents', methods=['POST'])
def create_banc_document(banc_id):
    BancConfig.query.get_or_404(banc_id)
    fitxer = request.files.get('document')
    if not fitxer or not fitxer.filename:
        return jsonify({'error': 'Cal adjuntar un fitxer'}), 400
    try:
        result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/bancs')
        url = result.get('secure_url')
        doc = BancDocument(
            banc_id=banc_id,
            document_url=url,
            document_nom=fitxer.filename,
            document_data=request.form.get('document_data') or None,
            notes=request.form.get('notes') or None,
        )
        db.session.add(doc)
        db.session.commit()
        return jsonify(doc.to_dict()), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/bancs/documents/<int:doc_id>', methods=['DELETE'])
def delete_banc_document(doc_id):
    doc = BancDocument.query.get_or_404(doc_id)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/despeses/<int:despesa_id>/documents', methods=['GET'])
def get_despesa_documents(despesa_id):
    docs = DespesaDocument.query.filter_by(despesa_id=despesa_id).order_by(DespesaDocument.creat_el.asc()).all()
    return jsonify([d.to_dict() for d in docs])

@app.route('/api/despeses/<int:despesa_id>/documents', methods=['POST'])
def create_despesa_document(despesa_id):
    Despesa.query.get_or_404(despesa_id)
    fitxers = uploaded_documents()
    if not fitxers:
        return jsonify({'error': 'Cal adjuntar un fitxer'}), 400
    try:
        docs = []
        for fitxer in fitxers:
            result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/despeses')
            doc = DespesaDocument(
                despesa_id=despesa_id,
                document_url=result.get('secure_url'),
                document_nom=fitxer.filename,
                notes=request.form.get('notes') or None,
            )
            db.session.add(doc)
            docs.append(doc)
        db.session.commit()
        return jsonify([doc.to_dict() for doc in docs]), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/despeses/documents/<int:doc_id>', methods=['DELETE'])
def delete_despesa_document(doc_id):
    doc = DespesaDocument.query.get_or_404(doc_id)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/factures/<int:factura_id>/documents', methods=['GET'])
def get_factura_documents(factura_id):
    docs = FacturaDocument.query.filter_by(factura_id=factura_id).order_by(FacturaDocument.creat_el.asc()).all()
    return jsonify([d.to_dict() for d in docs])

@app.route('/api/factures/<int:factura_id>/documents', methods=['POST'])
def create_factura_document(factura_id):
    Factura.query.get_or_404(factura_id)
    fitxer = request.files.get('document')
    if not fitxer or not fitxer.filename:
        return jsonify({'error': 'Cal adjuntar un fitxer'}), 400
    try:
        result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/factures')
        doc = FacturaDocument(
            factura_id=factura_id,
            document_url=result.get('secure_url'),
            document_nom=fitxer.filename,
            notes=request.form.get('notes') or None,
        )
        db.session.add(doc)
        db.session.commit()
        return jsonify(doc.to_dict()), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/factures/documents/<int:doc_id>', methods=['DELETE'])
def delete_factura_document(doc_id):
    doc = FacturaDocument.query.get_or_404(doc_id)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})

def documents_personals_autoritzat():
    return bool(session.get('documents_personals_ok'))

@app.route('/api/documents-personals/verificar-pin', methods=['POST'])
def verificar_pin_documents_personals():
    data = request.get_json()
    config = ConfigApp.query.filter_by(clau='pin_documents_personals').first()
    if not config or config.valor == data.get('pin', ''):
        session['documents_personals_ok'] = True
        return jsonify({'ok': True})
    session.pop('documents_personals_ok', None)
    return jsonify({'ok': False}), 401

@app.route('/api/documents-personals/pin', methods=['POST'])
def set_pin_documents_personals():
    if not session.get('auth'):
        return jsonify({'error': 'No autoritzat'}), 401
    data = request.get_json()
    nou_pin = data.get('pin', '').strip()
    if not nou_pin:
        return jsonify({'error': 'Cal una contrasenya'}), 400
    config = ConfigApp.query.filter_by(clau='pin_documents_personals').first()
    if config:
        config.valor = nou_pin
    else:
        config = ConfigApp(clau='pin_documents_personals', valor=nou_pin)
        db.session.add(config)
    db.session.commit()
    session['documents_personals_ok'] = True
    return jsonify({'ok': True})

@app.route('/api/documents-personals', methods=['GET'])
def get_documents_personals():
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    categoria = request.args.get('categoria')
    q = DocumentPersonal.query
    if categoria:
        q = q.filter(DocumentPersonal.categoria == categoria)
    docs = q.order_by(DocumentPersonal.data_document.desc().nullslast(), DocumentPersonal.creat_el.desc()).all()
    return jsonify([d.to_dict() for d in docs])

@app.route('/api/documents-personals', methods=['POST'])
def create_document_personal():
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    fitxers = uploaded_documents()
    if not fitxers:
        return jsonify({'error': 'Cal adjuntar un fitxer'}), 400
    nom = (request.form.get('nom') or '').strip()
    if not nom:
        return jsonify({'error': 'Cal indicar un nom'}), 400
    try:
        data_doc = datetime.strptime(request.form.get('data_document', ''), '%Y-%m-%d').date() if request.form.get('data_document') else None
        fitxer = fitxers[0]
        result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/documents-personals')
        doc = DocumentPersonal(
            nom=nom,
            categoria=request.form.get('categoria') or 'Administratiu',
            data_document=data_doc,
            entitat=request.form.get('entitat') or None,
            notes=request.form.get('notes') or None,
            document_url=result.get('secure_url'),
            document_nom=fitxer.filename,
            solucionat=(request.form.get('solucionat') == 'true'),
            data_solucio=datetime.strptime(request.form.get('data_solucio', ''), '%Y-%m-%d').date() if request.form.get('data_solucio') else None,
        )
        db.session.add(doc)
        db.session.flush()
        for fitxer_extra in fitxers[1:]:
            pujat = pujar_arxiu_cloudinary(fitxer_extra, 'gestiodespeses/documents-personals')
            db.session.add(DocumentPersonalAdjunt(
                document_personal_id=doc.id,
                document_url=pujat.get('secure_url'),
                document_nom=fitxer_extra.filename,
            ))
        db.session.commit()
        return jsonify(doc.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/documents-personals/<int:id>', methods=['PUT'])
def update_document_personal(id):
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    doc = DocumentPersonal.query.get_or_404(id)
    try:
        data_doc = datetime.strptime(request.form.get('data_document', ''), '%Y-%m-%d').date() if request.form.get('data_document') else None
        doc.nom = (request.form.get('nom') or doc.nom).strip()
        doc.categoria = request.form.get('categoria') or 'Administratiu'
        doc.data_document = data_doc
        doc.entitat = request.form.get('entitat') or None
        doc.notes = request.form.get('notes') or None
        doc.solucionat = request.form.get('solucionat') == 'true'
        doc.data_solucio = datetime.strptime(request.form.get('data_solucio', ''), '%Y-%m-%d').date() if request.form.get('data_solucio') else None
        for fitxer in uploaded_documents():
            result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/documents-personals')
            db.session.add(DocumentPersonalAdjunt(
                document_personal_id=doc.id,
                document_url=result.get('secure_url'),
                document_nom=fitxer.filename,
            ))
        db.session.commit()
        return jsonify(doc.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/documents-personals/<int:id>/adjunts', methods=['POST'])
def add_document_personal_adjunts(id):
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    doc = DocumentPersonal.query.get_or_404(id)
    fitxers = uploaded_documents()
    if not fitxers:
        return jsonify({'error': 'Cal seleccionar almenys un fitxer'}), 400
    try:
        afegits = []
        for fitxer in fitxers:
            result = pujar_arxiu_cloudinary(fitxer, 'gestiodespeses/documents-personals')
            adjunt = DocumentPersonalAdjunt(
                document_personal_id=doc.id,
                document_url=result.get('secure_url'),
                document_nom=fitxer.filename,
            )
            db.session.add(adjunt)
            afegits.append(adjunt)
        db.session.commit()
        return jsonify({'ok': True, 'afegits': len(afegits), 'document': doc.to_dict()}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/documents-personals/<int:id>', methods=['DELETE'])
def delete_document_personal(id):
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    doc = DocumentPersonal.query.get_or_404(id)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/documents-personals/adjunts/<int:adjunt_id>', methods=['DELETE'])
def delete_document_personal_adjunt(adjunt_id):
    if not documents_personals_autoritzat():
        return jsonify({'error': 'No autoritzat'}), 401
    adjunt = DocumentPersonalAdjunt.query.get_or_404(adjunt_id)
    db.session.delete(adjunt)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/bancs/fotografies', methods=['GET'])
def get_fotografies():
    banc = request.args.get('banc')
    q = FotografiaBanc.query
    if banc:
        q = q.filter_by(banc=banc)
    return jsonify([f.to_dict() for f in q.order_by(FotografiaBanc.data, FotografiaBanc.id).all()])

@app.route('/api/bancs/fotografies', methods=['POST'])
def create_fotografia():
    data = request.get_json()
    try:
        data_foto = datetime.strptime(data['data'], '%Y-%m-%d').date()
        banc_nom  = data['banc']
        saldo     = float(data['saldo'])
        nota      = data.get('nota', '')
        # Si ja existeix registre per aquest banc+data, actualitzar
        existent = FotografiaBanc.query.filter_by(banc=banc_nom, data=data_foto).first()
        if existent:
            existent.saldo = saldo
            existent.nota  = nota
            db.session.commit()
            return jsonify(existent.to_dict())
        foto = FotografiaBanc(data=data_foto, banc=banc_nom, saldo=saldo, nota=nota)
        db.session.add(foto)
        db.session.commit()
        return jsonify(foto.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/bancs/fotografies/<int:id>', methods=['PUT'])
def update_fotografia(id):
    foto = FotografiaBanc.query.get_or_404(id)
    data = request.get_json()
    try:
        foto.data  = datetime.strptime(data['data'], '%Y-%m-%d').date()
        foto.banc  = data['banc']
        foto.saldo = float(data['saldo'])
        foto.nota  = data.get('nota', '')
        db.session.commit()
        return jsonify(foto.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/bancs/fotografies/<int:id>', methods=['DELETE'])
def delete_fotografia(id):
    foto = FotografiaBanc.query.get_or_404(id)
    db.session.delete(foto)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/bancs/taula', methods=['GET'])
def get_bancs_taula():
    """
    Taula dinàmica: files=bancs, columnes=dates.
    Cada cel·la = saldo real introduït per l'usuari.
    Fila groga = variació entre data anterior i actual.
    Columna TOTAL VARIACIÓ = última - primera fotografia.
    """
    # Dates úniques, ordenades
    dates_q = db.session.query(FotografiaBanc.data).distinct().order_by(FotografiaBanc.data).all()
    dates = [r.data for r in dates_q]

    bancs_amb_fotos_q = db.session.query(FotografiaBanc.banc).distinct().all()
    bancs_amb_fotos = {r.banc for r in bancs_amb_fotos_q}
    bancs_cfg = BancConfig.query.order_by(BancConfig.ordre, BancConfig.id).all()
    banc_noms = [b.nom for b in bancs_cfg if b.nom in bancs_amb_fotos]

    if not dates:
        return jsonify({'bancs': banc_noms, 'dates': [], 'files': {}, 'totals': [], 'variacions': [], 'total_variacio': {}})

    # Construir diccionari {banc: {data_iso: saldo}}
    totes = FotografiaBanc.query.all()
    index = {}
    for f in totes:
        if f.banc not in index:
            index[f.banc] = {}
        index[f.banc][f.data.isoformat()] = float(f.saldo)

    # Files: per cada banc, saldo en cada data (None si no hi ha registre)
    files = {}
    for b in banc_noms:
        files[b] = [index.get(b, {}).get(d.isoformat()) for d in dates]

    # Totals per columna (suma bancs amb registre)
    totals = []
    for i in range(len(dates)):
        t = sum(files[b][i] for b in banc_noms if files[b][i] is not None)
        totals.append(round(t, 2))

    # Variació entre columnes consecutives
    variacions = []
    for i in range(len(dates)):
        if i == 0:
            variacions.append(None)
        else:
            variacions.append(round(totals[i] - totals[i-1], 2))

    # Total variació per banc: última - penúltima fotografia amb valor
    total_variacio = {}
    for b in banc_noms:
        valors = [v for v in files[b] if v is not None]
        total_variacio[b] = round(valors[-1] - valors[-2], 2) if len(valors) >= 2 else 0.0
    total_variacio['__total__'] = round(totals[-1] - totals[-2], 2) if len(totals) >= 2 else 0.0

    return jsonify({
        'bancs':          banc_noms,
        'dates':          [d.isoformat() for d in dates],
        'files':          files,
        'totals':         totals,
        'variacions':     variacions,
        'total_variacio': total_variacio,
    })


def prestec_xavi_resum():
    pagaments = PrestecXaviPagament.query.order_by(
        PrestecXaviPagament.data_pagament.asc(),
        PrestecXaviPagament.id.asc()
    ).all()
    total_retornat = sum(float(p.import_rebut or 0) for p in pagaments)
    saldo_pendent = PRESTEC_XAVI_IMPORT_INICIAL - total_retornat
    percent_retornat = total_retornat / PRESTEC_XAVI_IMPORT_INICIAL if PRESTEC_XAVI_IMPORT_INICIAL else 0
    return {
        'prestador': 'Maria Carmen Santisteban Sibila',
        'prestatari': 'Xavier Valcarce Santisteban',
        'import_inicial': PRESTEC_XAVI_IMPORT_INICIAL,
        'finalitat': 'Amortització parcial hipoteca',
        'interes': '0% (préstec sense interès)',
        'total_retornat': round(total_retornat, 2),
        'saldo_pendent': round(saldo_pendent, 2),
        'percent_retornat': round(percent_retornat, 4),
        'pagaments_count': len(pagaments),
        'pagaments': [p.to_dict() for p in pagaments],
    }


@app.route('/api/prestec-xavi', methods=['GET'])
def get_prestec_xavi():
    return jsonify(prestec_xavi_resum())


@app.route('/api/prestec-xavi/pagaments', methods=['POST'])
def create_prestec_xavi_pagament():
    data = request.get_json() or {}
    try:
        pagament = PrestecXaviPagament(
            data_pagament=datetime.strptime(data['data_pagament'], '%Y-%m-%d').date(),
            concepte=(data.get('concepte') or 'Devolució préstec').strip(),
            import_rebut=float(data['import_rebut']),
            compte=(data.get('compte') or '').strip(),
            notes=(data.get('notes') or '').strip(),
        )
        db.session.add(pagament)
        db.session.commit()
        return jsonify(pagament.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/prestec-xavi/pagaments/<int:id>', methods=['PUT'])
def update_prestec_xavi_pagament(id):
    pagament = PrestecXaviPagament.query.get_or_404(id)
    data = request.get_json() or {}
    try:
        pagament.data_pagament = datetime.strptime(data['data_pagament'], '%Y-%m-%d').date()
        pagament.concepte = (data.get('concepte') or 'Devolució préstec').strip()
        pagament.import_rebut = float(data['import_rebut'])
        pagament.compte = (data.get('compte') or '').strip()
        pagament.notes = (data.get('notes') or '').strip()
        db.session.commit()
        return jsonify(pagament.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@app.route('/api/prestec-xavi/pagaments/<int:id>', methods=['DELETE'])
def delete_prestec_xavi_pagament(id):
    pagament = PrestecXaviPagament.query.get_or_404(id)
    db.session.delete(pagament)
    db.session.commit()
    return jsonify({'ok': True})

# ─── Export / Import JSON ─────────────────────────────────────────────────────

@app.route('/api/exportar-json')
def exportar_json():
    dades = backup_complet_dades()
    return Response(
        json.dumps(dades, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=gestiodespeses_backup.json"}
    )

def _backup_json_value(value):
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode('ascii')
    if hasattr(value, 'quantize'):
        return float(value)
    return value

def _backup_table(model):
    primary_key = list(model.__table__.primary_key.columns)[0]
    return [
        {col.name: _backup_json_value(getattr(row, col.name)) for col in model.__table__.columns}
        for row in model.query.order_by(primary_key).all()
    ]

def backup_complet_dades():
    model_names = [
        'Despesa', 'Factura', 'TornOfici', 'TornComunicacio',
        'BancDocument', 'BancConfig', 'DespesaDocument', 'FacturaDocument',
        'DocumentPersonal', 'FotografiaBanc', 'PrestecXaviPagament', 'ConfigApp', 'Credencial',
        'CompteIban', 'Targeta', 'Asseguranca',
    ]
    taules = {}
    for name in model_names:
        model = globals().get(name)
        if model is not None:
            taules[model.__tablename__] = _backup_table(model)
    return {
        "format": "gestiodespeses_backup_complet_v2",
        "exported_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "counts": {name: len(items) for name, items in taules.items()},
        "tables": taules,
    }

@app.route('/api/backup-complet-download')
def backup_complet_download():
    if not session.get('auth'):
        return redirect(url_for('login', next=request.full_path))

    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    dades = backup_complet_dades()
    filename = f'backup_gestiocss_complet_{stamp}.tar.gz'
    tmp = tempfile.NamedTemporaryFile(prefix='gestiocss_backup_', suffix='.tar.gz', delete=False)
    tmp.close()

    readme = (
        'COPIA DE RESCAT COMPLETA - GESTIO CSS / GESTIODESPESES\n'
        f'Data: {datetime.now().strftime("%d/%m/%Y %H:%M")}\n\n'
        'Aquest arxiu s ha descarregat des del boto "Copia seguretat" de GestioCSS.\n\n'
        'Conte:\n'
        '- Copia JSON completa de les dades\n'
        '- Fitxers principals del codi disponible al servidor\n\n'
        'Comptatges:\n' +
        ''.join(f'- {name}: {count}\n' for name, count in dades["counts"].items()) +
        '\nAquest arxiu conte dades sensibles. Guarda l en un lloc segur.\n'
    ).encode('utf-8')
    data_json = json.dumps(dades, ensure_ascii=False, indent=2).encode('utf-8')

    with tarfile.open(tmp.name, 'w:gz') as tar:
        info = tarfile.TarInfo('LLEGEIX-ME_BACKUP.txt')
        info.size = len(readme)
        tar.addfile(info, io.BytesIO(readme))

        info = tarfile.TarInfo(f'copies_dades/gestiocss_backup_complet_{stamp}.json')
        info.size = len(data_json)
        tar.addfile(info, io.BytesIO(data_json))

        base_dir = os.path.abspath(os.path.dirname(__file__))
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'venv', '.venv'}]
            for name in files:
                if name.endswith(('.pyc', '.db')) or name.startswith('.env'):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, base_dir)
                tar.add(path, arcname=os.path.join('codi_gestiocss', rel))

    @after_this_request
    def cleanup(response):
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        return response

    return send_file(tmp.name, mimetype='application/gzip', as_attachment=True, download_name=filename)

@app.route('/api/exportar-json-antic')
def exportar_json_antic():
    despeses = Despesa.query.order_by(Despesa.id).all()
    factures = Factura.query.order_by(Factura.id).all()
    torn_comunicacions = TornComunicacio.query.order_by(TornComunicacio.id).all()
    def d(val):
        return str(val) if val else ""
    dades = {
        "despeses": [
            {"id": e.id, "data": d(e.data), "descripcio": e.descripcio,
             "categoria": e.categoria, "subcategoria": e.subcategoria or "", "tipus": e.tipus,
             "import": float(e.import_) if e.import_ else 0,
             "proveidor": e.proveidor, "notes": e.notes,
             "document_nom": e.document_nom, "document_url": e.document_url}
            for e in despeses
        ],
        "factures": [
            {"id": f.id, "numero": f.numero, "data": d(f.data),
             "client_nom": f.client_nom, "client_nif": f.client_nif,
             "client_adreca": f.client_adreca, "concepte": f.concepte,
             "base": float(f.base) if f.base else 0,
             "iva_pct": float(f.iva_pct) if f.iva_pct else 0,
             "irpf_pct": float(f.irpf_pct) if f.irpf_pct else 0,
             "notes": f.notes, "estat": f.estat,
             "document_nom": f.document_nom, "document_url": f.document_url}
            for f in factures
        ],
        "torn_comunicacions": [
            {"id": c.id, "data": d(c.data), "tipus": c.tipus,
             "tema": c.tema or c.assumpte, "assumpte": c.assumpte,
             "notes": c.notes, "document_nom": c.document_nom,
             "document_url": c.document_url}
            for c in torn_comunicacions
        ],
    }
    return Response(
        json.dumps(dades, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=gestiodespeses_backup.json"}
    )


@app.route('/importar', methods=['GET'])
def importar_page():
    return """<!DOCTYPE html>
<html lang="ca">
<head><meta charset="UTF-8"><title>Importar dades - GestióDespeses</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#1e293b;border-radius:16px;padding:40px;max-width:500px;width:100%;box-shadow:0 4px 32px #0004}
h1{font-size:1.4rem;margin-bottom:8px;color:#f8fafc}
p{color:#94a3b8;font-size:.9rem;margin-bottom:24px}
input[type=file]{width:100%;padding:12px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;margin-bottom:16px}
button{width:100%;padding:12px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:1rem;cursor:pointer}
button:hover{background:#1d4ed8}
#resultat{margin-top:20px;padding:12px;border-radius:8px;display:none}
.ok{background:#14532d;color:#86efac}
.err{background:#7f1d1d;color:#fca5a5}
a{color:#60a5fa;text-decoration:none;font-size:.85rem}
</style></head>
<body><div class="box">
<h1>📥 Importar dades</h1>
<p>Selecciona el fitxer <strong>gestiodespeses_backup.json</strong> per restaurar totes les despeses i factures.</p>
<input type="file" id="fitxer" accept=".json">
<button onclick="importar()">Importar ara</button>
<div id="resultat"></div>
<br><br><a href="/">← Tornar al dashboard</a>
</div>
<script>
async function importar(){
  const f=document.getElementById('fitxer').files[0];
  if(!f){alert('Selecciona un fitxer JSON');return}
  const text=await f.text();
  let dades;
  try{dades=JSON.parse(text)}catch(e){mostra('Format JSON invàlid','err');return}
  const r=await fetch('/api/importar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(dades)});
  const res=await r.json();
  if(res.ok) mostra('✓ '+res.missatge,'ok');
  else mostra('Error: '+res.error,'err');
}
function mostra(msg,cls){
  const el=document.getElementById('resultat');
  el.textContent=msg;el.className=cls;el.style.display='block';
}
</script></div></body></html>"""

@app.route('/api/importar', methods=['POST'])
def importar_dades():
    from datetime import date
    dades = request.get_json()
    if not dades:
        return jsonify({'ok': False, 'error': 'Cap dada rebuda'})
    importats = 0
    omesos = 0
    def parse_date(s):
        try:
            return date.fromisoformat(s) if s else None
        except:
            return None
    for d in dades.get('despeses', []):
        try:
            db.session.add(Despesa(
                data=parse_date(d.get('data','')),
                descripcio=d.get('descripcio',''),
                categoria=d.get('categoria',''),
                subcategoria=d.get('subcategoria',''),
                tipus=d.get('tipus','professional'),
                import_=d.get('import',0),
                proveidor=d.get('proveidor',''),
                notes=d.get('notes',''),
                document_nom=d.get('document_nom',''),
                document_url=d.get('document_url','')
            ))
            db.session.commit()
            importats += 1
        except Exception:
            db.session.rollback()
            omesos += 1
    for f in dades.get('factures', []):
        try:
            db.session.add(Factura(
                numero=f.get('numero',''),
                data=parse_date(f.get('data','')),
                client_nom=f.get('client_nom',''),
                client_nif=f.get('client_nif',''),
                client_adreca=f.get('client_adreca',''),
                concepte=f.get('concepte',''),
                base=f.get('base',0),
                iva_pct=f.get('iva_pct',21),
                irpf_pct=f.get('irpf_pct',0),
                notes=f.get('notes',''),
                estat=f.get('estat','pendent'),
                document_nom=f.get('document_nom',''),
                document_url=f.get('document_url','')
            ))
            db.session.commit()
            importats += 1
        except Exception:
            db.session.rollback()
            omesos += 1
    for c in dades.get('torn_comunicacions', []):
        try:
            assumpte = c.get('assumpte','')
            db.session.add(TornComunicacio(
                data=parse_date(c.get('data','')) or date.today(),
                tipus=c.get('tipus','email'),
                tema=c.get('tema') or assumpte,
                assumpte=assumpte,
                notes=c.get('notes',''),
                document_nom=c.get('document_nom',''),
                document_url=c.get('document_url','')
            ))
            db.session.commit()
            importats += 1
        except Exception:
            db.session.rollback()
            omesos += 1
    return jsonify({'ok': True, 'missatge': f'{importats} registres importats, {omesos} omesos'})


# ─── Model Credencials ────────────────────────────────────────────────────────

class Credencial(db.Model):
    __tablename__ = 'credencials'
    id          = db.Column(db.Integer, primary_key=True)
    categoria   = db.Column(db.String(100), nullable=False)
    servei      = db.Column(db.String(200), nullable=False)
    usuari      = db.Column(db.String(300))
    contrasenya = db.Column(db.String(500))
    notes       = db.Column(db.Text)
    creat_el    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':          self.id,
            'categoria':   self.categoria,
            'servei':      self.servei,
            'usuari':      self.usuari or '',
            'contrasenya': self.contrasenya or '',
            'notes':       self.notes or '',
        }

@app.route('/api/credencials/verificar-pin', methods=['POST'])
def verificar_pin_credencials():
    data = request.get_json()
    config = ConfigApp.query.filter_by(clau='pin_credencials').first()
    if not config or config.valor == data.get('pin', ''):
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401

@app.route('/api/credencials/pin', methods=['POST'])
def set_pin_credencials():
    if not session.get('auth'):
        return jsonify({'error': 'No autoritzat'}), 401
    data = request.get_json()
    nou_pin = data.get('pin', '').strip()
    if not nou_pin:
        return jsonify({'error': 'Cal un PIN'}), 400
    config = ConfigApp.query.filter_by(clau='pin_credencials').first()
    if config:
        config.valor = nou_pin
    else:
        config = ConfigApp(clau='pin_credencials', valor=nou_pin)
        db.session.add(config)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/credencials', methods=['GET'])
def get_credencials():
    creds = Credencial.query.order_by(Credencial.categoria, Credencial.servei).all()
    return jsonify([c.to_dict() for c in creds])

@app.route('/api/credencials', methods=['POST'])
def create_credencial():
    data = request.get_json()
    try:
        cred = Credencial(
            categoria   = data['categoria'],
            servei      = data['servei'],
            usuari      = data.get('usuari', ''),
            contrasenya = data.get('contrasenya', ''),
            notes       = data.get('notes', ''),
        )
        db.session.add(cred)
        db.session.commit()
        return jsonify(cred.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/credencials/<int:id>', methods=['PUT'])
def update_credencial(id):
    cred = Credencial.query.get_or_404(id)
    data = request.get_json()
    try:
        cred.categoria   = data['categoria']
        cred.servei      = data['servei']
        cred.usuari      = data.get('usuari', '')
        cred.contrasenya = data.get('contrasenya', '')
        cred.notes       = data.get('notes', '')
        db.session.commit()
        return jsonify(cred.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/credencials/<int:id>', methods=['DELETE'])
def delete_credencial(id):
    cred = Credencial.query.get_or_404(id)
    db.session.delete(cred)
    db.session.commit()
    return jsonify({'ok': True})



# ─── API Comptes IBAN ─────────────────────────────────────────────────────────

class CompteIban(db.Model):
    __tablename__ = 'comptes_iban'
    id      = db.Column(db.Integer, primary_key=True)
    entitat = db.Column(db.Text, nullable=False)
    nom     = db.Column(db.Text, nullable=False)
    iban    = db.Column(db.Text, nullable=False)
    notes   = db.Column(db.Text, default='')
    ordre   = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {'id': self.id, 'entitat': self.entitat, 'nom': self.nom,
                'iban': self.iban, 'notes': self.notes or '', 'ordre': self.ordre}

@app.route('/api/comptes-iban', methods=['GET'])
def get_comptes_iban():
    comptes = CompteIban.query.order_by(CompteIban.ordre, CompteIban.id).all()
    return jsonify([c.to_dict() for c in comptes])

@app.route('/api/comptes-iban', methods=['POST'])
def create_compte_iban():
    d = request.get_json()
    c = CompteIban(
        entitat = d.get('entitat','').strip(),
        nom     = d.get('nom','').strip(),
        iban    = d.get('iban','').replace(' ','').upper(),
        notes   = d.get('notes','').strip(),
        ordre   = CompteIban.query.count()
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201

@app.route('/api/comptes-iban/<int:id>', methods=['DELETE'])
def delete_compte_iban(id):
    c = CompteIban.query.get_or_404(id)
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


# ─── API Targetes ─────────────────────────────────────────────────────────────

class Targeta(db.Model):
    __tablename__ = 'targetes'
    id        = db.Column(db.Integer, primary_key=True)
    entitat   = db.Column(db.Text, nullable=False)
    nom       = db.Column(db.Text, nullable=False)
    numero    = db.Column(db.Text, nullable=False)
    caducitat = db.Column(db.Text, nullable=False)
    cvv       = db.Column(db.Text, nullable=True, default='')  # Camp desactivat per seguretat PCI DSS
    xarxa     = db.Column(db.Text, default='')
    tipus     = db.Column(db.Text, default='fisica')
    ordre     = db.Column(db.Integer, default=0)
    pin       = db.Column(db.Text, default='')

    def to_dict(self):
        return {
            'id': self.id, 'entitat': self.entitat, 'nom': self.nom,
            'numero': self.numero, 'caducitat': self.caducitat,
            'cvv': self.cvv, 'xarxa': self.xarxa, 'tipus': self.tipus, 'pin': self.pin or '',
        }

@app.route('/api/targetes', methods=['GET'])
def get_targetes():
    ts = Targeta.query.order_by(Targeta.ordre, Targeta.id).all()
    return jsonify([t.to_dict() for t in ts])

@app.route('/api/targetes', methods=['POST'])
def create_targeta():
    d = request.get_json()
    t = Targeta(
        entitat   = d.get('entitat','').strip(),
        nom       = d.get('nom','').strip(),
        numero    = d.get('numero','').replace(' ',''),
        caducitat = d.get('caducitat','').strip(),
        cvv       = d.get('cvv','').strip(),
        xarxa     = d.get('xarxa','').strip(),
        tipus     = d.get('tipus','fisica').strip(),
        pin       = d.get('pin','').strip(),
        ordre     = Targeta.query.count()
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201

@app.route('/api/targetes/<int:id>', methods=['PUT'])
def update_targeta(id):
    t = Targeta.query.get_or_404(id)
    d = request.get_json() or {}
    if 'entitat' in d: t.entitat = d['entitat'].strip()
    if 'nom' in d: t.nom = d['nom'].strip()
    if 'numero' in d: t.numero = d['numero'].replace(' ','')
    if 'caducitat' in d: t.caducitat = d['caducitat'].strip()
    if 'cvv' in d: t.cvv = d['cvv'].strip()
    if 'xarxa' in d: t.xarxa = d['xarxa'].strip()
    if 'tipus' in d: t.tipus = d['tipus'].strip()
    if 'pin' in d: t.pin = d['pin'].strip()
    db.session.commit()
    return jsonify(t.to_dict())

@app.route('/api/targetes/<int:id>', methods=['DELETE'])
def delete_targeta(id):
    t = Targeta.query.get_or_404(id)
    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})



@app.route('/api/targetes/esborrar-tots-cvv', methods=['POST'])
def esborrar_tots_cvv():
    """Esborra tots els CVV de totes les targetes. Operació one-shot per PCI DSS."""
    targetes = Targeta.query.all()
    n = 0
    for t in targetes:
        if t.cvv:
            t.cvv = ''
            n += 1
    db.session.commit()
    return jsonify({'ok': True, 'targetes_modificades': n})



# ─── Model Assegurances ───────────────────────────────────────────────────────────────────────────────

class Asseguranca(db.Model):
    __tablename__ = 'assegurances'

    id             = db.Column(db.Integer, primary_key=True)
    nom            = db.Column(db.String(200), nullable=False)
    companyia      = db.Column(db.String(200))
    numero_polissa = db.Column(db.String(100))
    tipus          = db.Column(db.String(50), nullable=False, default='Altres')
    titular        = db.Column(db.String(200))
    matricula      = db.Column(db.String(20))
    adreca         = db.Column(db.String(300))
    data_inici     = db.Column(db.Date)
    data_venciment = db.Column(db.Date)
    data_pagament  = db.Column(db.Date)
    periodicitat   = db.Column(db.String(20))
    import_prima   = db.Column(db.Numeric(10, 2))
    activa         = db.Column(db.Boolean, default=True)
    document_url   = db.Column(db.String(500))
    document_nom   = db.Column(db.String(200))
    data_itv       = db.Column(db.Date)
    notes          = db.Column(db.Text)
    creat_el       = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        documents = []
        if self.document_url:
            documents.append({
                'id': 'principal',
                'document_url': self.document_url,
                'document_nom': self.document_nom or 'Document',
                'principal': True,
            })
        for doc in AssegurancaDocument.query.filter_by(asseguranca_id=self.id).order_by(AssegurancaDocument.creat_el.asc()).all():
            doc_dict = doc.to_dict()
            doc_dict['principal'] = False
            documents.append(doc_dict)
        return {
            'id':             self.id,
            'nom':            self.nom,
            'companyia':      self.companyia or '',
            'numero_polissa': self.numero_polissa or '',
            'tipus':          self.tipus or 'Altres',
            'titular':        self.titular or '',
            'matricula':      self.matricula or '',
            'adreca':         self.adreca or '',
            'data_inici':     self.data_inici.isoformat() if self.data_inici else '',
            'data_venciment': self.data_venciment.isoformat() if self.data_venciment else '',
            'data_pagament':  self.data_pagament.isoformat() if self.data_pagament else '',
            'periodicitat':   self.periodicitat or '',
            'import_prima':   float(self.import_prima) if self.import_prima else 0,
            'activa':         self.activa if self.activa is not None else True,
            'document_url':   self.document_url or '',
            'document_nom':   self.document_nom or '',
            'documents':       documents,
            'data_itv':       self.data_itv.isoformat() if self.data_itv else '',
            'notes':          self.notes or '',
        }


class AssegurancaDocument(db.Model):
    __tablename__ = 'asseguranca_documents'
    id              = db.Column(db.Integer, primary_key=True)
    asseguranca_id  = db.Column(db.Integer, db.ForeignKey('assegurances.id'), nullable=False)
    document_url    = db.Column(db.String(500), nullable=False)
    document_nom    = db.Column(db.String(200))
    notes           = db.Column(db.Text)
    creat_el        = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'asseguranca_id': self.asseguranca_id,
            'document_url': self.document_url,
            'document_nom': self.document_nom or '',
            'notes': self.notes or '',
        }


# ─── API Assegurances ───────────────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    try:
        from sqlalchemy import text as _text
        db.session.execute(_text('ALTER TABLE assegurances ADD COLUMN IF NOT EXISTS data_itv DATE'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS incloure_renda BOOLEAN DEFAULT FALSE'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS renda_exercici INTEGER'))
        db.session.execute(_text('ALTER TABLE despeses ADD COLUMN IF NOT EXISTS excloure_renda BOOLEAN DEFAULT FALSE'))
        db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/api/assegurances/alertes', methods=['GET'])
def get_alertes_assegurances():
    avui = date.today()
    limit = avui + timedelta(days=30)
    alertes = Asseguranca.query.filter(
        Asseguranca.activa == True,
        db.or_(
            db.and_(Asseguranca.data_venciment != None, Asseguranca.data_venciment <= limit),
            db.and_(Asseguranca.data_itv != None, Asseguranca.data_itv <= limit),
        )
    ).order_by(Asseguranca.data_venciment, Asseguranca.data_itv).all()
    return jsonify([a.to_dict() for a in alertes])

@app.route('/api/assegurances/calendar.ics', methods=['GET'])
def assegurances_calendar():
    if request.args.get('token') != CALENDAR_TOKEN:
        abort(404)

    assegurances = Asseguranca.query.filter(
        Asseguranca.activa == True
    ).order_by(Asseguranca.data_venciment).all()
    ara = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//GestioDespeses//Venciments//CA',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:GSS venciments',
        'X-WR-CALDESC:Venciments, renovacions, subscripcions, assegurances i ITV de Gestio GSS',
        'X-WR-TIMEZONE:Europe/Madrid',
    ]

    def afegir_event(a, tipus_event, data_event, titol_prefix):
        if not data_event:
            return
        data_fi = data_event + timedelta(days=1)
        vehicle_adreca = a.matricula or a.adreca or ''
        uid_hash = hashlib.sha1(f'{a.id}-{tipus_event}-{data_event}'.encode('utf-8')).hexdigest()[:12]
        descripcio = '\n'.join(filter(None, [
            f'Tipus: {a.tipus or "Altres"}',
            f'Companyia: {a.companyia}' if a.companyia else '',
            f'Titular: {a.titular}' if a.titular else '',
            f'Referencia: {a.numero_polissa}' if a.numero_polissa else '',
            f'Vehicle/adreca: {vehicle_adreca}' if vehicle_adreca else '',
            f'Import: {a.import_prima} EUR' if a.import_prima else '',
            a.notes or '',
        ]))
        titol = f'{titol_prefix}: {a.nom}'
        lines.extend([
            'BEGIN:VEVENT',
            f'UID:gss-asseguranca-{a.id}-{tipus_event}-{uid_hash}@gestiodespeses',
            f'DTSTAMP:{ara}',
            f'DTSTART;VALUE=DATE:{_ics_date(data_event)}',
            f'DTEND;VALUE=DATE:{_ics_date(data_fi)}',
            f'SUMMARY:{_ics_text(titol)}',
            f'DESCRIPTION:{_ics_text(descripcio)}',
            f'LOCATION:{_ics_text(vehicle_adreca)}',
            'STATUS:CONFIRMED',
            'TRANSP:TRANSPARENT',
        ])
        for trigger in ('-P30D', '-P7D', '-P1D'):
            lines.extend([
                'BEGIN:VALARM',
                'ACTION:DISPLAY',
                f'DESCRIPTION:{_ics_text(titol)}',
                f'TRIGGER:{trigger}',
                'END:VALARM',
            ])
        lines.extend([
            'END:VEVENT',
        ])

    for asseguranca in assegurances:
        afegir_event(asseguranca, 'venciment', asseguranca.data_venciment, 'Venciment')
        afegir_event(asseguranca, 'itv', asseguranca.data_itv, 'ITV')

    lines.append('END:VCALENDAR')
    return Response(
        '\r\n'.join(lines) + '\r\n',
        content_type='text/calendar; charset=utf-8',
        headers={
            'Content-Disposition': 'inline; filename="gss-assegurances.ics"',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
        },
    )

@app.route('/api/assegurances', methods=['GET'])
def get_assegurances():
    assegurances = Asseguranca.query.order_by(Asseguranca.nom).all()
    return jsonify([a.to_dict() for a in assegurances])

@app.route('/api/assegurances', methods=['POST'])
def create_asseguranca():
    data = request.form
    fitxers = uploaded_documents()
    document_url = None
    document_nom = None
    if fitxers:
        try:
            result = pujar_arxiu_cloudinary(fitxers[0], 'gestiodespeses/assegurances')
            document_url = result.get('secure_url')
            document_nom = fitxers[0].filename
        except Exception as e:
            pass
    try:
        def parse_d(s):
            return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        a = Asseguranca(
            nom            = data['nom'],
            companyia      = data.get('companyia', ''),
            numero_polissa = data.get('numero_polissa', ''),
            tipus          = data.get('tipus', 'Altres'),
            titular        = data.get('titular', ''),
            matricula      = data.get('matricula', ''),
            adreca         = data.get('adreca', ''),
            data_inici     = parse_d(data.get('data_inici', '')),
            data_venciment = parse_d(data.get('data_venciment', '')),
            data_pagament  = parse_d(data.get('data_pagament', '')),
            periodicitat   = data.get('periodicitat', ''),
            import_prima   = float(data['import_prima']) if data.get('import_prima') else None,
            activa         = data.get('activa', 'true').lower() == 'true',
            data_itv       = parse_d(data.get('data_itv', '')),
            notes          = data.get('notes', ''),
            document_url   = document_url,
            document_nom   = document_nom,
        )
        db.session.add(a)
        db.session.flush()
        for fitxer_extra in fitxers[1:]:
            try:
                result_extra = pujar_arxiu_cloudinary(fitxer_extra, 'gestiodespeses/assegurances')
                if result_extra.get('secure_url'):
                    db.session.add(AssegurancaDocument(
                        asseguranca_id=a.id,
                        document_url=result_extra.get('secure_url'),
                        document_nom=fitxer_extra.filename,
                    ))
            except Exception:
                pass
        db.session.commit()
        return jsonify(a.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/assegurances/<int:id>', methods=['PUT'])
def update_asseguranca(id):
    a = Asseguranca.query.get_or_404(id)
    data = request.form
    fitxers = uploaded_documents()
    if fitxers:
        try:
            fitxers_per_afegir = fitxers
            if not a.document_url:
                result = pujar_arxiu_cloudinary(fitxers[0], 'gestiodespeses/assegurances')
                a.document_url = result.get('secure_url')
                a.document_nom = fitxers[0].filename
                fitxers_per_afegir = fitxers[1:]
            for fitxer_extra in fitxers_per_afegir:
                result_extra = pujar_arxiu_cloudinary(fitxer_extra, 'gestiodespeses/assegurances')
                if result_extra.get('secure_url'):
                    db.session.add(AssegurancaDocument(
                        asseguranca_id=a.id,
                        document_url=result_extra.get('secure_url'),
                        document_nom=fitxer_extra.filename,
                    ))
        except Exception:
            pass
    try:
        def parse_d(s):
            return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        a.nom            = data['nom']
        a.companyia      = data.get('companyia', '')
        a.numero_polissa = data.get('numero_polissa', '')
        a.tipus          = data.get('tipus', 'Altres')
        a.titular        = data.get('titular', '')
        a.matricula      = data.get('matricula', '')
        a.adreca         = data.get('adreca', '')
        a.data_inici     = parse_d(data.get('data_inici', ''))
        a.data_venciment = parse_d(data.get('data_venciment', ''))
        a.data_pagament  = parse_d(data.get('data_pagament', ''))
        a.periodicitat   = data.get('periodicitat', '')
        a.import_prima   = float(data['import_prima']) if data.get('import_prima') else None
        a.activa         = data.get('activa', 'true').lower() == 'true'
        a.data_itv       = parse_d(data.get('data_itv', ''))
        a.notes          = data.get('notes', '')
        db.session.commit()
        return jsonify(a.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/assegurances/<int:id>', methods=['DELETE'])
def delete_asseguranca(id):
    a = Asseguranca.query.get_or_404(id)
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(debug=True, port=5001)
