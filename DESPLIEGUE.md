# Puesta en producción (PC con Windows)

Resultado: panel en `https://redes.midominio.com`, con login de Cloudflare,
sin abrir puertos y arrancando solo con el PC.

Orden recomendado (el paso 1 puede tardar horas en propagarse, empieza por ahí):

1. DNS en Cloudflare
2. API key de OpenAI y prueba
3. Entorno Python y `.env`
4. Cloudflare Access (login), **antes** del túnel
5. Cloudflare Tunnel
6. Arranque automático
7. Comprobaciones

---

## 1. DNS del dominio en Cloudflare

En CMD:

```
nslookup -type=ns midominio.com
```

- Si salen servidores `*.ns.cloudflare.com`: ya está, pasa al paso 2.
- Si no, hay que migrarlo:
  1. dash.cloudflare.com → **Add a domain** → plan **Free**.
  2. Revisa que haya importado **todos** los registros DNS. Si tienes correo con el
     dominio, comprueba sobre todo los **MX** y **TXT**. Si no importó alguno, cópialo
     desde tu proveedor actual.
  3. Para no cambiar nada de lo que ya funciona (web, correo), deja los registros
     importados en **DNS only** (nube gris).
  4. En tu registrador (donde compraste el dominio): **desactiva DNSSEC** y cambia los
     nameservers por los 2 que te da Cloudflare.
  5. Espera a que Cloudflare marque el dominio como **Active** (suele tardar menos de
     1 h, como mucho 24 h).

## 2. API key de OpenAI

1. platform.openai.com → **Billing**: añade saldo. Sin saldo la API no funciona
   (la suscripción de ChatGPT no sirve para esto).
2. **Limits**: pon un límite de gasto mensual.
3. **API keys** → Create → cópiala en `.env` (paso 3).
4. Si al generar da un error de verificación: Settings → Organization → **Verify**
   (los modelos de imagen lo piden).

Prueba (tras el paso 3), sin tocar el panel:

```
.venv\Scripts\python ia_fondo.py "C:\ruta\a\una_foto.jpg" "atardecer dorado, ambiente de carrera" story
```

Si todo va bien, crea `prueba_ia.png`.

## 3. Entorno Python y `.env`

En la carpeta del proyecto:

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt waitress openai python-dotenv
copy .env.example .env
notepad .env
```

En `.env` van las claves (OpenAI, Telegram). El panel las lee al arrancar.
No hace falta definir variables de entorno en Windows.

## 4. Cloudflare Access (login)

El panel no tiene contraseña: **no publiques el túnel sin esto**.

1. Cloudflare → **Zero Trust**. La primera vez te pide un nombre de equipo y un plan:
   elige **Free** (hasta 50 usuarios).
2. **Access controls → Applications → Create new application → Self-hosted**.
3. Nombre: `Redes Racecore`. Dominio público: `redes` . `midominio.com`.
4. Política: **Allow**, *Include* → **Emails** → tu email (y el de quien más quieras).
   Nunca pongas *Everyone* ni *One-time PIN* sin una lista de emails.
5. Método de login: **One-time PIN** (te llega un código por email).
6. Duración de sesión: 1 mes, para no meter el código a diario.
7. Guarda.

## 5. Cloudflare Tunnel

1. Instala cloudflared (CMD **como administrador**):
   ```
   winget install --id Cloudflare.cloudflared
   ```
2. Cloudflare → **Networking → Tunnels → Create a tunnel** → tipo **Cloudflared** →
   nombre `racecore-pc` → sistema **Windows**.
3. Copia el comando que te da (`cloudflared.exe service install eyJ...`) y ejecútalo en
   una CMD **nueva como administrador**. Esto lo instala como servicio de Windows:
   arranca solo con el PC.
4. El túnel debe salir como **Healthy**.
5. En el túnel → **Routes → Add route → Published application**:
   - Subdominio: `redes`, dominio: `midominio.com`
   - Service URL: `http://localhost:5000`

   Cloudflare crea el registro DNS automáticamente.

## 6. Arranque automático del panel

El programador solo funciona con el panel en marcha, así que va como tarea de Windows:
arranca al encender el PC (sin iniciar sesión) y se relanza si se cae.

PowerShell **como administrador**, en la carpeta del proyecto:

```
powershell -ExecutionPolicy Bypass -File windows\instalar_servicio.ps1
```

- Log: `logs\panel.log`
- **Tras cambiar `app.py` o `.env`**, vuelve a ejecutar `instalar_servicio.ps1`
  (para lo que haya y arranca de cero).
- Para quitarlo: `windows\desinstalar_servicio.ps1`.
- No lances `python app.py` a mano con la tarea activa (chocan en el puerto 5000).

Y en Windows: Configuración → Sistema → Energía → **Suspender: Nunca**.
Con el PC apagado o suspendido no se genera nada.

## 7. Comprobaciones

- Ventana de incógnito → `https://redes.midominio.com` → pide email y código → panel.
- Desde el móvil con **datos** (no wifi): lo mismo.
- Desde otro equipo de tu red, `http://IP-del-PC:5000` **no** debe abrir (el panel solo
  escucha en el propio PC; se entra únicamente por el túnel).
- Reinicia el PC **sin** iniciar sesión → el panel responde desde el móvil.

---

## Prompt IA (por publicación)

- Vacío: se usa la foto tal cual (como ahora).
- Con texto: la IA rehace el **fondo** a partir de una foto al azar de la categoría. El
  texto se sigue poniendo por código.
- Siempre se añaden estas reglas: sin texto ni logos, el tercio inferior limpio y la esquina
  superior derecha despejada (ver `REGLAS` en `ia_fondo.py`).
- Si la IA falla (sin saldo, sin clave, caída...), sale la foto original y el post no se pierde.
- Ejemplos: `atardecer dorado, luz cálida, ambiente de competición` ·
  `noche con focos, asfalto mojado con reflejos` · `cielo despejado, colores vivos, verano`.
- Cada generación es una llamada de pago (incluido «Generar ahora»). Calidad y modelo
  en `.env` (`OPENAI_IMAGE_QUALITY`, `OPENAI_IMAGE_MODEL`).
