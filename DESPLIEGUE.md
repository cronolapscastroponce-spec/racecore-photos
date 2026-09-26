# Panel de redes de Racecore: instalación

Resultado final: el panel en `https://redes.midominio.com`, con login de Cloudflare, sin
abrir puertos y arrancando solo con el PC.

## A. En el PC

1. **Descomprimir** el zip en `C:\racecore-redes`. No lo pongas en Documentos ni en el
   Escritorio: OneDrive los sincroniza y da problemas.
2. Doble clic en **`1_instalar.bat`**. Instala Python si hace falta y las librerías.
3. Doble clic en **`2_probar.bat`**. Se abre el panel en el navegador
   (`http://localhost:5000`). Mientras esa ventana negra siga abierta, el panel funciona.
4. En **Ajustes**: sube tu **logo** y tu **banner de redes** (si tienen fondo negro, el panel se lo quita),
   o escribe tus redes para que el panel las dibuje.
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
  - **Varias versiones**: en Título, Franja y Texto del post, sepáralas con una línea `---` y
    el panel las va turnando (1ª, 2ª, 3ª…). La 2ª del título sale con la 2ª del texto.
  - **Prompt IA**: si lo dejas vacío, se usa la foto tal cual. Si escribes algo, la IA
    rehace solo el fondo; el texto lo pone siempre el panel.
- **Listas para publicar**: las imágenes aparecen solas antes de su hora. Desde ahí:
  - Descargar la imagen.
  - Copiar el texto.
  - Compartir (desde el móvil).
  - «Otra foto».
  - Marcar como publicada.
- **Ajustes**: la clave de OpenAI y Telegram (opcional). El estado del programador.

## B0. Tipos de publicación

Al crear o editar una publicación, lo primero es elegir **qué tipo es**, y el formulario solo
enseña lo de ese tipo: **Normal** (tus fotos y textos, los días que elijas), **Eventos**,
**Horario de apertura** o **Deportes en TV**. En Publicaciones salen agrupadas por tipo.

## B1. Horario de apertura (fines de semana, puentes, fiestas)

1. **Publicaciones → + Horario de apertura**: sale una publicación ya preparada (en pausa).
2. En **Horario de apertura** eliges los días («el fin de semana siguiente» o unas fechas),
   la apertura y el cierre (10 a 20) y qué hay abierto: Kart Rental, Entrenos Motos,
   Entrenos Karting (o las que escribas), cada una con sus horas si son distintas.
3. Si esos días hay un evento (Racecore, CKS o a mano), se quitan las horas que ocupa.
   En **Eventos** eliges para cada uno qué ocupa: mañana (hasta las 16:00, lo normal),
   todo el día, tarde (desde las 16:00) o nada. La hora de corte se cambia en Ajustes.
4. Variables: `{horario}`, `{dias_horario}` y `{eventos_horario}`. Con el diseño
   «Cartel con lista» el horario sale en el centro de la imagen.
5. En cualquier publicación puedes marcar **fotos de varias categorías**.

## B1b. Deportes en TV para la cafetería (F1, MotoGP, fútbol)

- **Publicaciones → + F1 y MotoGP**: los jueves a las 12:00, con la clasificación, el sprint y
  la carrera de jueves a domingo que caigan dentro del horario de la cafetería (10 a 20).
- **Publicaciones → + Fútbol**: se revisa cada día a las 10:00 y **solo se publica si ese día**
  juegan los equipos elegidos (Real Madrid, Barcelona) por el canal elegido (DAZN), sin las
  competiciones excluidas (Liga F) y acabando antes del cierre (un partido son unas 2 horas).
- Los horarios salen de Marca: los calendarios de F1 y MotoGP y su guía de TV (que solo trae
  hoy y mañana). Si no hay nada que cumpla las condiciones, no se publica.
- En Ajustes → Estado se ve la última lectura; si Marca cambia su página, sale un aviso.
- **Fotos de cada deporte**: en «Deportes en TV» eliges una categoría para F1, otra para MotoGP y
  otra para fútbol; se usa la del deporte que salga ese día. Si no eliges, el panel dibuja un
  fondo genérico (campo de fútbol, o asfalto con pianos y bandera a cuadros). No uses fotos
  oficiales de internet: tienen derechos.
- Variables: `{deportes}` y `{dias_deportes}`.

## B2. Eventos (Racecore y CKS)

El panel lee los eventos de Racecore y de CKS por la red del circuito y prepara solas las
publicaciones de cada evento: recordatorios, horarios, inscritos, resultados…

1. En Racecore (y en CKS): genera el **token de lectura** en su configuración y cópialo.
2. En el panel, **Ajustes → Racecore y CKS**: pon la IP de cada PC (ej. `192.168.1.50`; el puerto
   se pone solo: 8100 en Racecore, 8090 en CKS) y su token. Guarda: el panel prueba la conexión.
3. Pestaña **Eventos**: salen los eventos de los dos. Se actualizan cada 10 minutos.
   - Si alguno no responde, se siguen usando los últimos datos y se avisa.
   - También puedes crear **eventos a mano** (para lo que no esté en Racecore ni en CKS).
4. Publicaciones para eventos: en **Eventos → Crear las 5 de ejemplo**. Se crean **en pausa**:
   elige la categoría de fotos, prueba con «Generar ahora» y actívalas.
   - Son publicaciones normales con el modo **Eventos**: antes, el mismo día o después del
     evento; «de 14 a 3 días, cada 2» publica a 14, 12, 10, 8, 6 y 4 días.
   - **Condición**: siempre, solo con la inscripción abierta, solo si quedan plazas o solo
     cuando haya resultados (si aún no los hay, lo vuelve a mirar hasta la hora).
   - **¿Para qué eventos?**: marca uno o varios campeonatos (valen también para sus carreras
     futuras) y/o eventos sueltos, o escribe un texto. Sin marcar nada, para todos. Debajo dice
     a qué eventos se aplica.
   - **Fotos por evento**: en la pestaña Eventos, a cada evento le puedes elegir su categoría
     de fotos (motos, alquiler…). Si no eliges, usa la de cada publicación.
   - **Duplicar** (en Publicaciones) hace una copia en pausa para crear variantes. Si borras
     alguna de las de ejemplo, en Eventos sale un botón para volver a crearla.
   - Variables: `{evento}` `{campeonato}` `{dia}` `{fecha}` `{hora}` (del evento), `{dias}`,
     `{faltan}` («en 5 días», «mañana»), `{inscritos}` `{plazas}` `{libres}` `{precio}`,
     `{enlace}` (inscripción), `{web}`, `{horarios}`, `{resultados}`, `{ganadores}` y `{pilotos}`.
   - **Nombres de los inscritos**: `{pilotos}` (uno por línea, por categorías) y el diseño
     **Cartel con lista**, que los pone en el centro de la imagen en columnas. En Ajustes eliges
     «Ana Pérez» o «Ana P.».
   - Resultados: pon poca antelación (el ejemplo usa 600 min) para que salgan con la carrera acabada.
     «Otra foto» la rehace con los datos de ese momento.
- Datos personales: el panel solo guarda nombre, fecha, horarios, plazas, número de inscritos,
  precio, enlaces, el podio y el **nombre para redes** de cada inscrito (nada más de ellos).

## B3. Fotos de las tandas de alquiler (CKS)

1. En **Ajustes → Racecore y CKS**, en «Fotos de las tandas de alquiler», elige
   «Traerlas a una categoría nueva: Tandas CKS» (o una categoría tuya) y guarda.
2. Cada 10 minutos el panel trae las fotos nuevas. CKS solo da las de tandas en las que
   **todos** los pilotos dieron permiso de imagen, y ningún nombre.
3. Se guardan **una semana** y se borran solas; también si alguien retira el permiso.
4. Úsalas como cualquier categoría: por ejemplo, una publicación diaria «Así fue hoy en pista»
   con esa categoría de fotos. En la página de la categoría está «Traer fotos ahora».

## C. Clave de OpenAI (para el Prompt IA)

1. Entra en platform.openai.com → **Billing** y añade saldo. La suscripción de ChatGPT no sirve.
2. Ve a **Limits** y pon un límite de gasto mensual.
3. En **API keys** → **Create**, copia la clave y pégala en el panel, en **Ajustes**.
4. Si al generar sale un aviso de verificación: Settings → Organization → **Verify**.

Si la IA falla, la publicación sale con la foto original y el aviso se ve en «Listas».

## C2. Acceso desde el móvil o desde fuera (fácil): Tailscale

Sin tocar el dominio ni el correo. Solo entran tus dispositivos.

1. En el PC del panel: instala Tailscale (https://tailscale.com/download) y entra con tu
   cuenta (Google, Microsoft…).
2. En CMD **como administrador**: `tailscale serve --bg 5000`. La primera vez te da un enlace
   para activar HTTPS: ábrelo y acepta. Al final te dice la dirección del panel, del tipo
   `https://nombre-del-pc.xxxx.ts.net`.
3. En el móvil: instala la app Tailscale y entra con **la misma cuenta**.
4. Abre esa dirección en el móvil. Guárdala en la pantalla de inicio.
   Como es HTTPS, el botón «Compartir» manda la imagen directamente a Instagram.

El panel sigue escuchando solo en el propio PC: Tailscale hace de puente privado.
Para quitarlo: `tailscale serve --https=443 off`.

Si prefieres entrar desde cualquier navegador sin instalar nada, sigue con D, E y F (Cloudflare).

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
