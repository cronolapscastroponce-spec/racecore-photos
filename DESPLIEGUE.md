# Panel de redes de Racecore: instalación

Resultado final: el panel en `https://redes.midominio.com`, con login de Cloudflare, sin
abrir puertos y arrancando solo con el PC.

## A. En el PC

1. **Descomprimir** el zip en `C:\racecore-redes`. No lo pongas en Documentos ni en el
   Escritorio: OneDrive los sincroniza y da problemas.
2. Doble clic en **`1_instalar.bat`**. Instala Python si hace falta y las librerías.
3. Doble clic en **`2_probar.bat`**. Se abre el panel en el navegador
   (`http://localhost:5000`). Mientras esa ventana negra siga abierta, el panel funciona.
4. En **Ajustes**: sube tu **logo** (PNG sin fondo) y pon tus **redes**.
   - Fuentes propias (opcional): `Titulo.ttf` y `Texto.ttf` en la carpeta `fuentes`.

## Actualizar el panel

Doble clic en **`actualizar.bat`**. Baja la última versión sin tocar fotos, publicaciones ni ajustes.

## B. Usar el panel

- **Fotos**: crea categorías y sube fotos. Puedes elegir cientos a la vez: se suben de una en una.
- **Publicaciones**: días o fecha, hora, antelación, categoría, formato, diseño y textos.
  - Diseño **Cartel**: título grande (lo que va entre `*asteriscos*` sale en color), franja
    de color (la última línea sale más grande), datos separados con `|` y tus redes abajo.
  - Diseño **Sencillo**: texto abajo con una etiqueta de color.
  - Diseño **IA completa** (con clave de OpenAI): la IA hace el cartel con tus textos a partir de
    la foto; el panel pone siempre encima el logo y las redes. Si la IA falla, sale el Cartel.
  - Variables: `{dia}` (sábado), `{fecha}` (27 de septiembre) y `{hora}` (18:00).
  - **Prompt IA**: si lo dejas vacío, se usa la foto tal cual. Si escribes algo, la IA
    rehace solo el fondo; el texto lo pone siempre el panel.
- **Listas para publicar**: las imágenes aparecen solas antes de su hora. Desde ahí:
  - Descargar la imagen.
  - Copiar el texto.
  - Compartir (desde el móvil).
  - «Otra foto».
  - Marcar como publicada.
- **Ajustes**: la clave de OpenAI y Telegram (opcional). El estado del programador.

## C. Clave de OpenAI (para el Prompt IA)

1. Entra en platform.openai.com → **Billing** y añade saldo. La suscripción de ChatGPT no sirve.
2. Ve a **Limits** y pon un límite de gasto mensual.
3. En **API keys** → **Create**, copia la clave y pégala en el panel, en **Ajustes**.
4. Si al generar sale un aviso de verificación: Settings → Organization → **Verify**.

Si la IA falla, la publicación sale con la foto original y el aviso se ve en «Listas».

## D. Dominio en Cloudflare

En CMD: `nslookup -type=ns midominio.com`

- Si salen servidores `*.ns.cloudflare.com`: ya está.
- Si no, hay que migrarlo:
  1. En dash.cloudflare.com → **Add a domain** → plan **Free**.
  2. Revisa que importe **todos** los registros, sobre todo **MX/TXT** si tienes correo con
     ese dominio. Déjalos en **DNS only** (nube gris).
  3. En tu registrador: **desactiva DNSSEC** y pon los 2 nameservers que da Cloudflare.
  4. Espera a que salga **Active**. Suele tardar menos de 1 h; como mucho, 24 h.

## E. Login (Cloudflare Access): antes del túnel

1. Cloudflare → **Zero Trust**. Elige el plan **Free**.
2. **Access controls → Applications → Create new application → Self-hosted**.
3. Dominio: `redes` . `midominio.com`.
4. Política **Allow** → *Include* → **Emails** → tu email. No pongas *Everyone*.
5. Método de login: **One-time PIN**. Duración de la sesión: 1 mes.

## F. Túnel (Cloudflare Tunnel)

1. En CMD como administrador: `winget install --id Cloudflare.cloudflared`
2. Cloudflare → **Networking → Tunnels → Create a tunnel** → **Cloudflared** → **Windows**.
3. Copia el comando `cloudflared.exe service install ...` y ejecútalo en una CMD **nueva**
   como administrador. Queda como servicio y arranca solo.
4. En el túnel → **Routes → Add route → Published application**:
   - `redes` . `midominio.com`
   - Service URL: `http://localhost:5000`

## G. Que arranque siempre

1. Cierra la ventana de `2_probar.bat`.
2. Doble clic en **`3_arranque_automatico.bat`** y acepta el permiso de administrador.
   El panel arranca con Windows, sin iniciar sesión, y se relanza si se cae.
   El registro de lo que pasa queda en `logs\panel.log`.
3. En Windows: Configuración → Sistema → Energía → **Suspender: Nunca**.
4. Si cambias archivos del panel, vuelve a ejecutar `3_arranque_automatico.bat`.
5. Para quitarlo: `quitar_arranque_automatico.bat`.

## H. Comprobar

- Ventana de incógnito → `https://redes.midominio.com`: pide email y código, y luego abre el panel.
- Desde el móvil **con datos** (sin wifi): igual.
- Reinicia el PC **sin** iniciar sesión: el panel sigue respondiendo desde el móvil.
- Desde otro equipo de tu red, `http://IP-del-PC:5000` **no** abre. El panel solo
  escucha en el propio PC; desde fuera se entra únicamente por el túnel con login.
