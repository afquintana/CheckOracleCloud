# CheckOracleCloud

Comprobador de capacidad para `VM.Standard.A1.Flex` en Oracle Cloud Infrastructure (OCI), pensado para ejecutarse en GitHub Actions.

## Qué hace ahora

El repositorio mantiene el script `check_capacity.py` y añade un nuevo workflow multi-región.

- Descubre dinámicamente **todas las regiones** del realm actual mediante API oficial de OCI (`IdentityClient.list_regions`).
- Recorre cada región e intenta listar **todos los Availability Domains** accesibles.
- Consulta capacidad para:
  - shape: `VM.Standard.A1.Flex`
  - configuración: `4 OCPU / 24 GB`
- Continúa el escaneo aunque algunas regiones o ADs fallen por:
  - no suscripción de región,
  - permisos insuficientes,
  - shape no soportado,
  - errores temporales de red/API.
- Si hay uno o más hits (`AVAILABLE` con `available_count > 0`), envía **un único email resumen por ejecución**.

## Workflows

### 1) Workflow existente
- `.github/workflows/check-madrid3.yml`
- Mantiene el comportamiento anterior centrado en Madrid.

### 2) Nuevo workflow multi-región
- `.github/workflows/check-all-regions.yml`
- Triggers:
  - `schedule`: cada hora (`0 * * * *`)
  - `workflow_dispatch`: ejecución manual
- Ejecuta `python check_capacity.py` con instalación de dependencias (`oci`).
- Tiene permisos mínimos: `contents: read`.
- El workflow solo falla ante errores reales de ejecución/configuración del script; los errores parciales por región/AD se registran y se continúa.

## Secrets y variables requeridas

### GitHub Secrets (obligatorios)
- `OCI_USER_OCID`
- `OCI_TENANCY_OCID`
- `OCI_FINGERPRINT`
- `OCI_PRIVATE_KEY_PEM`
- `EMAIL_USER`
- `EMAIL_APP_PASSWORD`
- `EMAIL_TO`

### GitHub Variables (opcionales)
- `OCI_REGION` (bootstrap region para inicializar el cliente de Identity; por defecto `eu-madrid-3`)
- `SMTP_HOST` (por defecto `smtp.gmail.com`)
- `SMTP_PORT` (por defecto `587`)

## Ejecución local

1. Instala dependencias:

```bash
python -m pip install --upgrade pip oci
```

2. Exporta variables de entorno:

```bash
export OCI_USER_OCID="ocid1.user..."
export OCI_TENANCY_OCID="ocid1.tenancy..."
export OCI_FINGERPRINT="aa:bb:..."
export OCI_PRIVATE_KEY_PEM="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"

export OCI_REGION="eu-madrid-3"   # bootstrap

export EMAIL_USER="tu_correo@gmail.com"
export EMAIL_APP_PASSWORD="app-password"
export EMAIL_TO="destino@example.com"
export SMTP_HOST="smtp.gmail.com"  # opcional
export SMTP_PORT="587"             # opcional
```

3. Ejecuta:

```bash
python check_capacity.py
```

## Ejecución manual en GitHub (workflow_dispatch)

1. Ve a **Actions**.
2. Abre **Check OCI Capacity (All Regions)**.
3. Pulsa **Run workflow**.

## Formato del email (ejemplo)

Asunto:

```text
OCI capacidad disponible: 2 hit(s)
```

Cuerpo (resumen):

```text
Se detectó capacidad OCI para VM.Standard.A1.Flex (4 OCPU / 24 GB).

Repositorio: owner/repo
Región bootstrap (OCI_REGION): eu-madrid-3
Timestamp UTC: 2026-04-21 13:00:00 UTC
Timestamp Europe/Madrid: 2026-04-21 15:00:00 CEST
Número de hits: 2

region | availability_domain | status | available_count
-------------------------------------------------------
eu-frankfurt-1 | ... | AVAILABLE | 1
eu-amsterdam-1 | ... | AVAILABLE | 2
```

## Nota importante sobre Always Free

Este escaneo multi-región detecta regiones/ADs **accesibles desde la tenencia/realm actual**. Aun así, la posibilidad real de lanzar recursos Always Free sigue dependiendo de restricciones de tenencia (incluyendo home region), suscripción regional y permisos OCI.
