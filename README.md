# GestióDespeses

Aplicació web per gestionar despeses professionals i personals. Independent de LexGestió.

## Variables d'entorn a Railway

```
DATABASE_URL         → PostgreSQL (afegit automàticament pel plugin)
SECRET_KEY           → clau aleatòria (p.ex. openssl rand -hex 32)
CLOUDINARY_CLOUD_NAME → dpmml4jus
CLOUDINARY_API_KEY   → (de Cloudinary)
CLOUDINARY_API_SECRET → (de Cloudinary)
```

## Desplegament a Railway

1. Crear nou projecte a Railway
2. Afegir PostgreSQL plugin
3. Connectar repositori GitHub
4. Afegir les variables d'entorn
5. Deploy automàtic

## Funcionalitats

- Afegir/editar/eliminar despeses
- Categories professional i personal
- Adjuntar PDF/imatge via Cloudinary
- Filtres per any, mes, tipus, categoria
- Cerca en temps real
- Export CSV
- KPIs: total any, professional, personal, recompte
