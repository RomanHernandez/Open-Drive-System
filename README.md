# Open Drive System

Sistema unificado open-source para **inventariar, explorar, filtrar, descargar y gestionar múltiples cuentas de Google Drive** (ej. Trabajo + Personal), liberar espacio local en tu disco mediante **Google Drive for Desktop** y consultar/descargar tus archivos desde cualquier dispositivo móvil mediante **Cloudflare Workers + D1**.

> 🔒 **Privacidad y Seguridad**: Este repositorio/plantilla **no incluye ninguna credencial, correo, token ni base de datos**. Todos los archivos sensibles (`credentials.json`, `tokens/`, `drive.db` y `cloud/.secrets.json`) están excluidos en `.gitignore` y cada usuario los genera con su propia cuenta.

---

## Estructura del Proyecto

```text
open-drive-system/
├── drivedata.py               # Motor CLI de sincronización y búsqueda en SQLite
├── dashboard.py               # Panel web local para Mac/Desktop (http://127.0.0.1:8765)
├── requirements.txt           # Dependencias de Python (Flask + Google API Client)
├── setup.sh                   # Asistente interactivo de instalación paso a paso
├── export-template.sh         # Genera un .zip limpio sin datos personales para compartir
├── credentials.example.json   # Plantilla de ejemplo para OAuth de Google Cloud
└── cloud/                     # Proyecto Serverless (Cloudflare Workers + D1)
    ├── cloudflare.config.ts   # Configuración del Worker, D1 y Cron cada 4 horas
    ├── .secrets.example.json  # Plantilla de variables secretas para Cloudflare
    ├── package.json           # Dependencias del Worker
    └── src/
        └── index.ts           # Backend + UI móvil responsive + proxy de descarga
```

---

## Paso 1: Crear tus credenciales OAuth en Google Cloud (Gratis)

Cada persona que replique este proyecto debe crear su propio cliente OAuth en Google Cloud (toma 3 minutos):

1. Entra a [Google Cloud Console](https://console.cloud.google.com/) y crea un proyecto nuevo (ej. `open-drive-system`).
2. Ve a **APIs y servicios → Biblioteca**, busca **Google Drive API** y haz clic en **Habilitar**.
3. Ve a **APIs y servicios → Pantalla de consentimiento de OAuth**:
   - Selecciona **Externo** (External) y completa el nombre de la app (ej. `Open Drive System`) y tu correo.
   - En la sección **Usuarios de prueba (Test users)**, agrega los correos de las cuentas de Google Drive que vas a conectar (ej. tu cuenta de Trabajo y tu cuenta Personal).
4. Ve a **APIs y servicios → Credenciales → + Crear credenciales → ID de cliente de OAuth**:
   - Tipo de aplicación: **Aplicación de escritorio (Desktop app)**.
   - Descarga el archivo JSON y guárdalo en la raíz del proyecto con el nombre exacto:
     ```bash
     credentials.json
     ```
   *(Puedes guiarte por la estructura de `credentials.example.json`)*.

---

## Paso 2: Instalación y autorización local

Abre la Terminal en la carpeta del proyecto y ejecuta el asistente automático:

```bash
chmod +x setup.sh
./setup.sh
```

¿Qué hace `./setup.sh` automáticamente?
1. Crea el entorno virtual `.venv` e instala `requirements.txt`.
2. Abre el navegador para autorizar tus cuentas guardando los tokens en `tokens/cuenta1.json` y `tokens/cuenta2.json` con permisos privados (`700`).
3. Descarga el índice completo de archivos y carpetas en `drive.db`.
4. Si lo deseas, construye automáticamente tu archivo `cloud/.secrets.json` con tu propio `PANEL_PIN`.

### Comandos manuales útiles (`drivedata.py`)

```bash
# Autorizar una cuenta adicional
./.venv/bin/python drivedata.py add-account cuenta1
./.venv/bin/python drivedata.py add-account cuenta2

# Sincronizar todas las cuentas
./.venv/bin/python drivedata.py sync

# Ver estadísticas rápidas en terminal
./.venv/bin/python drivedata.py stats

# Programar sincronización automática diaria en macOS (launchd)
./.venv/bin/python drivedata.py install-schedule --hour 3
```

---

## Paso 3: Usar el Dashboard Local y liberar espacio del disco

### 3.1 Iniciar el panel local
```bash
./.venv/bin/python dashboard.py
```
Se abrirá automáticamente en **`http://127.0.0.1:8765`**. Desde ahí puedes:
- Explorar tus carpetas con diseño moderno y tarjetas de almacenamiento.
- Filtrar por tipo de archivo (`Imágenes / SVG`, `Videos`, `Diseño`, `PDF`, `Docs / Pages`, `Comprimidos`) y ordenar por fecha, peso o nombre con la barra dividida **`Filter | Sort`**.
- Detectar **Archivos grandes** y **Duplicados (MD5)** entre cuentas y enviarlos a la papelera de Drive.
- En la pestaña **En tu Mac**, ver qué archivos ocupan disco local y abrirlos en Finder para elegir **Clic derecho → Quitar descarga**.

### 3.2 (Opcional) Enlazar carpetas locales a Google Drive for Desktop
Para que tus carpetas locales vivan en la nube sin ocupar disco:
1. Instala [Google Drive para escritorio](https://www.google.com/drive/download/) e inicia sesión en tus cuentas en modo **Transmitir archivos (Stream files)**.
2. Mueve tus carpetas a `~/Library/CloudStorage/GoogleDrive-TU_CORREO/Mi unidad/` y crea un symlink:
   ```bash
   ln -s "$HOME/Library/CloudStorage/GoogleDrive-tu_correo_1@gmail.com/Mi unidad/Trabajo" "$HOME/Desktop/Trabajo"
   ln -s "$HOME/Library/CloudStorage/GoogleDrive-tu_correo_2@gmail.com/Mi unidad/Personal" "$HOME/Desktop/Personal"
   ```

---

## Paso 4: Desplegar en la Nube (Acceso móvil desde iPhone / Android)

El directorio `cloud/` contiene una aplicación **Cloudflare Workers + D1** (100% dentro del plan gratuito de Cloudflare) que se sincroniza sola cada 4 horas y te permite explorar, filtrar, enviar a la papelera y descargar archivos directamente a tu teléfono.

### 4.1 Instalar dependencias y autenticar Cloudflare CLI (`cf`)
```bash
cd cloud
npm install
npx cf login
```

### 4.2 Crear la base de datos Serverless D1
```bash
npx cf d1 create open-drive-db
```

### 4.3 Configurar `cloud/.secrets.json`
Si no lo generaste con `./setup.sh`, copia la plantilla y rellena tus valores:
```bash
cp .secrets.example.json .secrets.json
```
Edita `cloud/.secrets.json` con:
- `PANEL_PIN`: El código PIN que te pedirá el dashboard en el celular.
- `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET`: Del archivo `credentials.json`.
- `REFRESH_TOKEN_CUENTA1` y `REFRESH_TOKEN_CUENTA2`: El campo `refresh_token` dentro de `tokens/cuenta1.json` y `tokens/cuenta2.json`.

### 4.4 Desplegar a producción
```bash
npx cf deploy --secrets-file .secrets.json
```
Al terminar, la terminal te mostrará tu URL pública (`https://open-drive-system.<tu-subdominio>.workers.dev`).

### 4.5 Primera sincronización en la nube e ícono en móvil
1. Abre tu URL de Cloudflare Workers en Safari desde tu móvil.
2. Ingresa tu **PIN** y toca el botón **Sync** arriba a la derecha para poblar la base de datos D1 por primera vez (luego se actualizará sola cada 4 horas).
3. En Safari de iPhone, toca el botón **Compartir → Agregar a inicio (Add to Home Screen)**: se instalará como Web App a pantalla completa con su propio ícono HD.

---

## Cómo compartir este proyecto de forma segura

Si quieres generar un paquete limpio sin credenciales para compartirlo por correo o chat:

```bash
./export-template.sh
```

Esto generará el archivo `../open-drive-system-template.zip` conteniendo **únicamente** el código fuente, las plantillas `.example.json` y esta guía paso a paso.
