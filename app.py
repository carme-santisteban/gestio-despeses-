import os
import csv
import io
import base64
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
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

@app.route('/')
def index():
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

    if fitxer and fitxer.filename:
        try:
            result = cloudinary.uploader.upload(
                fitxer,
                resource_type='auto',
                folder='gestiodespeses'
            )
            document_url = result.get('secure_url')
            document_nom = fitxer.filename
        except Exception as e:
            pass  # Continua sense document si falla Cloudinary

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
        return jsonify(despesa.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400

@app.route('/api/despeses/<int:id>', methods=['PUT'])
def update_despesa(id):
    despesa = Despesa.query.get_or_404(id)
    data = request.form
    fitxer = request.files.get('document')

    if fitxer and fitxer.filename:
        try:
            result = cloudinary.uploader.upload(fitxer, resource_type='auto', folder='gestiodespeses')
            despesa.document_url = result.get('secure_url')
            despesa.document_nom = fitxer.filename
        except:
            pass

    try:
        despesa.data       = datetime.strptime(data['data'], '%Y-%m-%d').date()
        despesa.descripcio = data['descripcio']
        despesa.categoria  = data['categoria']
        despesa.tipus      = data.get('tipus', 'professional')
        despesa.import_    = float(data['import_'])
        despesa.proveidor  = data.get('proveidor', '')
        despesa.notes      = data.get('notes', '')
        db.session.commit()
        return jsonify(despesa.to_dict())
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

if __name__ == '__main__':
    app.run(debug=True, port=5001)
