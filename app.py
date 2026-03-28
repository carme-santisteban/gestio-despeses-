import os
import csv
import io
import base64
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import extract, func
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)

# Database
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/gestiodespeses')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

db = SQLAlchemy(app)

# Cloudinary config
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', '')
)

# ─── Models ───────────────────────────────────────────────────────────────────

class Despesa(db.Model):
    __tablename__ = 'despeses'

    id            = db.Column(db.Integer, primary_key=True)
    data          = db.Column(db.Date, nullable=False, default=date.today)
    descripcio    = db.Column(db.String(500), nullable=False)
    categoria     = db.Column(db.String(100), nullable=False)
    tipus         = db.Column(db.String(20), nullable=False, default='professional')  # professional / personal
    import_       = db.Column(db.Numeric(10, 2), nullable=False)
    proveidor     = db.Column(db.String(200))
    notes         = db.Column(db.Text)
    document_url  = db.Column(db.String(500))
    document_nom  = db.Column(db.String(200))
    creat_el      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':           self.id,
            'data':         self.data.isoformat() if self.data else None,
            'descripcio':   self.descripcio,
            'categoria':    self.categoria,
            'tipus':        self.tipus,
            'import_':      float(self.import_) if self.import_ else 0,
            'proveidor':    self.proveidor or '',
            'notes':        self.notes or '',
            'document_url': self.document_url or '',
            'document_nom': self.document_nom or '',
        }

# ─── Init DB ──────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/auth', methods=['POST'])
def auth():
    if request.json.get('pwd') == 'Css75917591':
        session['auth'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False}), 401


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == '2026gestio':
            session['auth'] = True
            return redirect('/')
        else:
            error = 'Contrasenya incorrecta'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('auth', None)
    return redirect('/login')
@app.route('/')
def index():
    if not session.get('auth'):
        return redirect('/login')
    return render_template('index.html')

# --- CRUD Despeses ---

@app.route('/api/despeses', methods=['GET'])
def get_despeses():
    any_  = request.args.get('any', type=int)
    mes   = request.args.get('mes', type=int)
    tipus = request.args.get('tipus')
    cat   = request.args.get('categoria')

    q = Despesa.query
    if any_:
        q = q.filter(extract('year', Despesa.data) == any_)
    if mes:
        q = q.filter(extract('month', Despesa.data) == mes)
    if tipus:
        q = q.filter(Despesa.tipus == tipus)
    if cat:
        q = q.filter(Despesa.categoria == cat)

    despeses = q.order_by(Despesa.data.desc()).all()
    return jsonify([d.to_dict() for d in despeses])

@app.route('/api/despeses', methods=['POST'])
def create_despesa():
    data = request.form
    fitxer = request.files.get('document')

    document_url = None
    document_nom = None
    cloudinary_error = None

    if fitxer and fitxer.filename:
        try:
            result = cloudinary.uploader.upload(
                fitxer,
                resource_type='raw',
                folder='gestiodespeses'
            )
            document_url = result.get('secure_url')
            document_nom = fitxer.filename
        except Exception as e:
            cloudinary_error = str(e)

    try:
        despesa = Despesa(
            data        = datetime.strptime(data['data'], '%Y-%m-%d').date(),
            descripcio  = data['descripcio'],
            categoria   = data['categoria'],
            tipus       = data.get('tipus', 'professional'),
            import_     = float(data['import_']),
            proveidor   = data.get('proveidor', ''),
            notes       = data.get('notes', ''),
            document_url= document_url,
            document_nom= document_nom,
        )
        db.session.add(despesa)
        db.session.commit()
        result = despesa.to_dict()
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
    fitxer = request.files.get('document')

    cloudinary_error = None
    if fitxer and fitxer.filename:
        try:
            result = cloudinary.uploader.upload(fitxer, resource_type='raw', folder='gestiodespeses')
            despesa.document_url = result.get('secure_url')
            despesa.document_nom = fitxer.filename
        except Exception as e:
            cloudinary_error = str(e)

    try:
        despesa.data       = datetime.strptime(data['data'], '%Y-%m-%d').date()
        despesa.descripcio = data['descripcio']
        despesa.categoria  = data['categoria']
        despesa.tipus      = data.get('tipus', 'professional')
        despesa.import_    = float(data['import_'])
        despesa.proveidor  = data.get('proveidor', '')
        despesa.notes      = data.get('notes', '')
        db.session.commit()
        result = despesa.to_dict()
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

# --- Estadístiques ---

@app.route('/api/estadistiques')
def estadistiques():
    any_ = request.args.get('any', datetime.now().year, type=int)

    # Totals per mes
    mensuals = db.session.query(
        extract('month', Despesa.data).label('mes'),
        Despesa.tipus,
        func.sum(Despesa.import_).label('total')
    ).filter(
        extract('year', Despesa.data) == any_
    ).group_by('mes', Despesa.tipus).all()

    # Totals per categoria
    categories = db.session.query(
        Despesa.categoria,
        func.sum(Despesa.import_).label('total')
    ).filter(
        extract('year', Despesa.data) == any_
    ).group_by(Despesa.categoria).all()

    # Any total
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

    q = Despesa.query
    if any_:
        q = q.filter(extract('year', Despesa.data) == any_)
    if mes:
        q = q.filter(extract('month', Despesa.data) == mes)
    if tipus:
        q = q.filter(Despesa.tipus == tipus)

    despeses = q.order_by(Despesa.data).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Data', 'Descripció', 'Categoria', 'Tipus', 'Import (€)', 'Proveïdor', 'Notes'])
    for d in despeses:
        writer.writerow([
            d.data.strftime('%d/%m/%Y'),
            d.descripcio,
            d.categoria,
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

# ─── Model Factura ────────────────────────────────────────────────────────────

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
    estat         = db.Column(db.String(20), default='pendent')  # pendent / pagada / proforma
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

def generar_numero_factura(data):
    any_ = data.year
    mes  = data.month
    prefix = f"{any_}{mes:02d}"
    count = Factura.query.filter(
        Factura.numero.like(f"{prefix}%")
    ).count()
    return f"{prefix}{count+1:02d}"

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
            result = cloudinary.uploader.upload(fitxer, resource_type='raw', folder='gestiodespeses/factures')
            document_url = result.get('secure_url')
            document_nom = fitxer.filename
        except Exception as e:
            pass
    try:
        data_factura = datetime.strptime(data['data'], '%Y-%m-%d').date()
        factura = Factura(
            numero        = generar_numero_factura(data_factura),
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
            result = cloudinary.uploader.upload(fitxer, resource_type='raw', folder='gestiodespeses/factures')
            factura.document_url = result.get('secure_url')
            factura.document_nom = fitxer.filename
        except:
            pass
    try:
        factura.data          = datetime.strptime(data['data'], '%Y-%m-%d').date()
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

@app.route('/api/factures/anys')
def get_anys_factures():
    anys = db.session.query(
        extract('year', Factura.data).label('any')
    ).distinct().order_by('any').all()
    return jsonify([int(r.any) for r in anys])


if __name__ == '__main__':
    app.run(debug=True, port=5001)
