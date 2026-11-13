# Fault Shop · Zabbix + APM (OpenTelemetry → ClickHouse)

[English](README.en.md)

Una tienda pequeña de microservicios en Docker con un **panel de fallas**: interruptores que ponen lenta la base de datos, hacen fallar las compras o apagan un servicio hasta que los apagas. Sirve para ver cómo se ve cada tipo de falla en **Zabbix** (ítems, triggers, dashboard) y en **Zabbix APM** (trazas y logs OpenTelemetry guardados en ClickHouse).

Se integra igual que la [demo de Manufactura](https://github.com/SimonLexRS/Zabbix-Demo-Manufactura): **solo hace falta la URL de Zabbix y un token de API**. Al arrancar, el contenedor `zabbix-bridge` importa el template, crea el grupo, el host de la tienda, el host APM y el dashboard, y empieza a enviar datos. El destino de las trazas se define con una variable (`OTEL_EXPORTER_OTLP_ENDPOINT`).

```mermaid
flowchart LR
    B["Navegador<br>shop-web (RUM)"] --> W["web<br>nginx"]
    L["loadgen<br>clientes simulados"] --> W
    W -->|/api| S["shop<br>Python"]
    S --> C["catalog<br>Node.js"]
    S --> K["checkout<br>Python"]
    S --> R[("Redis<br>carritos + cola")]
    C --> P[("PostgreSQL")]
    K --> C
    K --> Y["payment<br>Node.js"]
    K --> P
    K -->|"cola orders"| R
    R --> N["notifier<br>Python"]
    Y --> X["bank<br>servicio externo"]

    ZB["zabbix-bridge"] -. "health, Redis, PostgreSQL,<br>nginx, faults" .-> W & S & C & K & Y & X & R & P
    ZB ==>|"API: template, hosts, dashboard<br>history.push cada 10 s"| Z["Zabbix 7.0+"]
    S & C & K & Y & N ==>|"OTLP trazas + logs"| A["Proxy APM de Zabbix 8<br>u OTel Collector"]
    W ==>|"/otlp (RUM)"| A
    A --> H[("ClickHouse")]
    H --> Z
```

## Contenido

1. [Qué se crea en Zabbix](#1-qué-se-crea-en-zabbix)
2. [Despliegue en Dokploy](#2-despliegue-en-dokploy)
3. [Despliegue con Docker](#3-despliegue-con-docker)
4. [Destino APM (OpenTelemetry → ClickHouse)](#4-destino-apm-opentelemetry--clickhouse)
5. [Romper algo: el panel de fallas](#5-romper-algo-el-panel-de-fallas)
6. [Variables de entorno](#6-variables-de-entorno)
7. [Alternativa: Zabbix por HTTP agent (pull)](#7-alternativa-zabbix-por-http-agent-pull)
8. [Solución de problemas](#8-solución-de-problemas)
9. [Estructura y desarrollo](#9-estructura-y-desarrollo)

## 1. Qué se crea en Zabbix

Todo lo hace `zabbix-bridge` al arrancar y es idempotente: se puede redesplegar sin duplicar nada.

| Objeto | Nombre | Detalle |
|---|---|---|
| Grupo de templates | `Templates/Applications` | |
| Template | `Fault Shop by Zabbix sender` | 36 ítems trapper + regla LLD de servicios. Archivo: [`zabbix/template_fault_shop_sender.yaml`](zabbix/template_fault_shop_sender.yaml) |
| Grupo de hosts | `Fault Shop` (`ZABBIX_HOST_GROUP`) | |
| Host de la tienda | `Fault Shop` (`ZABBIX_HOST`) | Sin interfaz: los datos llegan por `history.push` |
| Host APM | `apm-fault-shop` (`ZABBIX_APM_HOST_NAME`) | Enlazado a `OpenTelemetry application by OTLP` con `{$OTLP.NAMESPACE}=fault-shop`. Solo si ese template existe en tu Zabbix |
| Dashboard | `Fault Shop · Estado y APM` | Página *Estado de la tienda* y página *APM y faults* |

**Ítems** (claves `fshop.*`, cada 10 s):

| Grupo | Ítems |
|---|---|
| Servicios (LLD `{#SVC}`: web, shop, catalog, checkout, payment, bank) | `fshop.svc.up[{#SVC}]`, `fshop.svc.rt_ms[{#SVC}]`, `fshop.svc.http_code[{#SVC}]` (health check `/health`) |
| Negocio | pedidos totales, pedidos por minuto, pedidos y ventas de los últimos 5 min, ticket medio |
| Redis | disponible, pedidos en cola `orders`, carritos abiertos, memoria, clientes, ops/s |
| PostgreSQL | disponible, tiempo de consulta, conexiones, tamaño de la base |
| nginx | conexiones activas/leyendo/escribiendo/en espera, peticiones por segundo (`stub_status`) |
| notifier | segundos desde su último ciclo (latido en Redis) |
| Panel de fallas | `fshop.fault[<switch>]` por cada interruptor, servicios apagados, switches activos |
| Bridge | `fshop.bridge.heartbeat` |

**Triggers**:

| Trigger | Severidad | Expresión (resumida) |
|---|---|---|
| Fault Shop: zabbix-bridge sin datos | High | `nodata(heartbeat,{$FAULTSHOP.NODATA})=1` — el resto depende de este |
| Servicio {#SVC}: caído | High | `max(svc.up[{#SVC}],#2)=0` |
| Servicio {#SVC}: health check lento | Warning | `min(svc.rt_ms[{#SVC}],1m)>{$FAULTSHOP.RT.MAX}` |
| Redis: caído / PostgreSQL: caído | Disaster | `max(up,#2)=0` |
| PostgreSQL: demasiadas conexiones | Warning | `min(connections,5m)>{$FAULTSHOP.PG.CONN.MAX}` |
| Cola de e-mails acumulada | Average | `min(queue_len,2m)>{$FAULTSHOP.QUEUE.MAX}` |
| notifier: sin actividad | High | `min(heartbeat_age,1m)>{$FAULTSHOP.NOTIFIER.MAX}` |
| Sin pedidos con clientes simulados activos | Average | `max(orders.per_min,{$FAULTSHOP.ORDERS.WINDOW})=0 and last(fault[load_rps])>0` |
| Fault inyectado: <switch> | Info | `last(fault[<switch>])>0`, tag `fault=<switch>` |
| Fault: servicios apagados desde el panel | Warning | `last(fault.down_count)>0` |

Los triggers de trazas (*Span duration is too big*, *High span error rate*, *No spans received*) los aporta el template OpenTelemetry de Zabbix APM en el host `apm-fault-shop`.

## 2. Despliegue en Dokploy

**Requisitos**: Zabbix **7.0 o superior** accesible por HTTPS desde el servidor de Dokploy, y (para APM) un receptor OTLP alcanzable desde los contenedores (ver [sección 4](#4-destino-apm-opentelemetry--clickhouse)).

1. **Token de API en Zabbix**: *Users → API tokens → Create API token* para un usuario con rol **Super admin** (necesita importar templates y crear hosts y dashboards). Copia el token.
2. **Dokploy → Project → Create Service → Compose**:
   - *Provider*: **Git** (o GitHub) → repositorio `https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop.git`, rama `main`.
   - *Compose Path*: `./compose.yaml`
   - *Compose Type*: Docker Compose.
3. **Environment** (pestaña *Environment* del servicio). Mínimo:

   ```env
   # Zabbix (destino de ítems, triggers, dashboard)
   ZABBIX_URL=https://zabbix.tu-dominio.com
   ZABBIX_API_TOKEN=<token del paso 1>

   # APM (destino de trazas y logs OpenTelemetry -> ClickHouse)
   OTEL_EXPORTER_OTLP_ENDPOINT=http://<proxy-apm-o-collector>:4317
   OTEL_EXPORTER_OTLP_PROTOCOL=grpc
   RUM_OTLP_HTTP_ENDPOINT=http://<collector>:4318
   ```

   El resto de variables tiene valores por defecto; la lista completa está en [`.env.example`](.env.example) y en la [sección 6](#6-variables-de-entorno).
4. **Domains**: *Add Domain* → *Service Name* **`web`**, *Container Port* **`80`**, host p. ej. `shop.tu-dominio.com`, HTTPS con Let's Encrypt si quieres.
5. **Deploy**. La primera construcción tarda 2–5 minutos.
6. **Verificar**:
   - Tienda: `https://shop.tu-dominio.com/` · panel de fallas: `https://shop.tu-dominio.com/faults.html`
   - Logs del servicio `zabbix-bridge` en Dokploy: `Provisión OK: {... 'host': 'host creado', 'apm': 'host APM creado'}` y luego `Dashboard: {... 'complete': True}` (≈1 min, cuando la LLD ya creó los ítems por servicio).
   - Zabbix → *Monitoring → Hosts* → `Fault Shop` con *Latest data*, y *Dashboards* → `Fault Shop · Estado y APM`.
   - Estado del bridge desde la terminal de Dokploy (contenedor `zabbix-bridge`): `wget -qO- localhost:8000/api/zabbix/status`.

> [!WARNING]
> El panel de fallas no tiene autenticación. Si publicas el dominio en Internet, protégelo con **Basic Auth** en Dokploy (*Advanced → Security*) o úsalo solo en una red de pruebas.

**Release defectuosa**: pon `CHECKOUT_VERSION=1.1.0` en *Environment* y *Deploy*; vuelve a `1.0.0` para hacer rollback (ver [sección 5](#fallas-que-no-son-un-interruptor)).

**Persistencia**: PostgreSQL usa el volumen `pgdata` (los pedidos sobreviven a redeploys). Redis no persiste a propósito.

## 3. Despliegue con Docker

```sh
git clone https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop.git && cd Zabbix-Demo-Fault-shop
cp .env.example .env        # edita ZABBIX_URL, ZABBIX_API_TOKEN y el destino OTEL_*
docker compose -f compose.yaml -f compose.local.yaml up -d --build
docker compose logs -f zabbix-bridge
```

| Página | Dirección |
|---|---|
| Tienda | `http://<IP>:18090/` (`SHOP_PORT`) |
| Panel de fallas | `http://<IP>:18090/faults.html` |
| Métricas del bridge (JSON) | `http://<IP>:18091/api/metrics` (`BRIDGE_PORT`) |
| Estado de la integración | `http://<IP>:18091/api/zabbix/status` |

`compose.local.yaml` solo publica puertos; en Dokploy no se usa porque el tráfico entra por su dominio (Traefik).

Sin `ZABBIX_URL` la tienda funciona igual y el bridge solo expone `/api/metrics`. Para probar sin Zabbix: `ZABBIX_DRY_RUN=1` (escribe en el log lo que enviaría).

## 4. Destino APM (OpenTelemetry → ClickHouse)

Los servicios `shop`, `catalog`, `checkout`, `payment` y `notifier` arrancan con el agente OpenTelemetry de su lenguaje (sin código de telemetría propio) y envían **trazas y logs** por OTLP. `bank` (servicio "externo") y `loadgen` no se instrumentan a propósito. Nada en la tienda conoce ClickHouse: eso lo hace el receptor.

Elige **uno** de estos destinos y ponlo en *Environment* (Dokploy) o en `.env`:

**A) Proxy de Zabbix 8.0 con colector APM** (recibe OTLP **gRPC** y escribe en ClickHouse)

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://zabbix-proxy-apm.tu-red:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

Configuración del proxy (referencia, en su propio compose):

```yaml
ZBX_STARTAPMCOLLECTOR: 1
ZBX_APMLISTENIP: 0.0.0.0
ZBX_APMLISTENPORT: 4317
ZBX_TELEMETRYPROVIDER_0: '{"provider":"clickhouse","url":"http://clickhouse:8123","db":"zabbix"}'
```

**B) OpenTelemetry Collector** (`otelcol-contrib` con el exporter `clickhouse`, OTLP **HTTP**)

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.tu-red:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

El Collector escribe `zabbix.otel_traces` y `zabbix.otel_logs`; Zabbix los lee como fuente de datos APM.

**Notas**

- Si el receptor está en el **mismo servidor** fuera de Docker, usa `http://host.docker.internal:4317|4318` (ya mapeado a la IP del host en el compose) y asegúrate de que escucha en la IP del bridge de Docker, no solo en `127.0.0.1`.
- Si está en **otra stack de Dokploy**, usa el nombre del contenedor/servicio en la red `dokploy-network` o la IP/DNS privada del servidor.
- Con TLS usa `https://...`; con autenticación, `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<token>`.
- **RUM (navegador)**: `web/otel.js` envía spans a `/otlp` del propio sitio y nginx los reenvía a `RUM_OTLP_HTTP_ENDPOINT`. Debe ser un receptor **OTLP/HTTP** (4318): el proxy APM de Zabbix solo escucha gRPC, así que para RUM hace falta un Collector (opción B) o un receptor HTTP. El nombre debe resolver cuando arranca nginx.
- **`service.namespace`** (`OTEL_SERVICE_NAMESPACE`, por defecto `fault-shop`) es la "aplicación" en Zabbix APM y es la macro `{$OTLP.NAMESPACE}` del host APM.
- Métricas OTel desactivadas por defecto (`OTEL_METRICS_EXPORTER=none`); pon `otlp` si tu receptor las guarda.

Comprobar que llegan trazas (en el servidor de ClickHouse):

```sql
SELECT ServiceName, count() AS spans, countIf(StatusCode = 'Error') AS errors
FROM zabbix.otel_traces
WHERE ResourceAttributes['service.namespace'] = 'fault-shop' AND Timestamp > now() - INTERVAL 5 MINUTE
GROUP BY ServiceName ORDER BY ServiceName;
```

Debe listar `catalog`, `checkout`, `notifier`, `payment`, `shop` (y `shop-web` tras abrir la tienda en un navegador).

## 5. Romper algo: el panel de fallas

Abre `/faults.html` y enciende una falla. Actúa en segundos, sobre los clientes simulados y sobre lo que hagas tú. **Volver todo a la normalidad** apaga todo. Cada switch activo abre el problema *Fault inyectado: …* (Info) en el host `Fault Shop`, así que en Zabbix siempre ves la **causa** junto a los **síntomas**.

| Interruptor | Qué hace | En Zabbix (host `Fault Shop`) | En APM (host `apm-fault-shop`) |
|---|---|---|---|
| **Base de datos lenta** (500 ms, 2 s, 6 s) | Cada consulta de `catalog` tarda ese tiempo | Sube *nginx: peticiones* en espera; con 6 s cae *pedidos por minuto* → *Sin pedidos con clientes simulados activos* | `GET /products` lento; el tiempo está en el span de PostgreSQL. *Span duration is too big* |
| **Consultas N+1** | La lista hace una consulta por producto | Sube *PostgreSQL: conexiones* | Traza de `GET /products`: 17 consultas en vez de 1 |
| **Pagos fallidos** (20, 50, 100 %) | `payment` rechaza esa parte de los cobros | Bajan *pedidos/min* y *ventas*; al 100 %, *Sin pedidos…* | `CardDeclinedError` en `payment`. *High span error rate* |
| **Banco lento** (1 s, 3 s, 6 s) | El servicio externo responde tarde (`payment` espera 4 s) | *Servicio bank: health check lento* si supera `{$FAULTSHOP.RT.MAX}` | Línea lenta `payment → bank`; con 6 s, `BankTimeoutError` |
| **Servicio caído** (`catalog`, `checkout`, `payment`, `bank`) | Responde 503; `bank` cierra la conexión | **Servicio X: caído** (High) + *Fault: servicios apagados* | Nodos en rojo; `BankUnreachableError`, `RuntimeError: payment answered HTTP 503` |
| **Un producto rompe el código** | Solo *Mystery box* (id 13) falla con `TypeError` | Info del fault | Grupo de errores en `GET /products/:id`, `app.product.id = 13` |
| **Sin existencias** | Las compras se rechazan con 409 | *Sin pedidos con clientes simulados activos* | 4xx: nadie "falló", pero sube la tasa de error entre servicios |
| **Checkout con CPU** (200 ms, 800 ms, 2 s) | `checkout` gasta ese tiempo en su código | Info del fault | Span `validate order` sin llamadas dentro |
| **Cola de e-mail lenta** (1 s, 5 s) | `notifier` tarda eso por pedido | **Cola de e-mails acumulada** y sube *notifier: segundos desde su último ciclo* | La traza se alarga en `orders process` |
| **E-mails fallan** | `notifier` no puede enviar | Info del fault | `ConnectionError` en `notifier` mientras las compras salen bien |
| **Error JavaScript en "Add to cart"** | La página se rompe al hacer clic | Info del fault | *Real user monitoring → Errors*: `TypeError` (haz clic tú) |
| **Clientes simulados** (0, 1, 2, 5 por segundo) | El tráfico de `loadgen` | Cambian *nginx: peticiones por segundo* y *pedidos por minuto* | Con 0: *No spans received* a los 10 min |

### Fallas que no son un interruptor

| Falla | Dokploy | Docker | Qué se ve |
|---|---|---|---|
| Caída real de un servicio (conexión rechazada) | Detén el contenedor `payment` desde la pestaña de contenedores | `docker compose stop payment` / `start payment` | *Servicio payment: caído* (código HTTP 0); `ConnectionError` en `checkout` |
| Release defectuosa | `CHECKOUT_VERSION=1.1.0` → Deploy | `CHECKOUT_VERSION=1.1.0 docker compose up -d checkout` | APM → `checkout` → *Versions*: 1.1.0 falla con `KeyError` en pedidos de 3+ productos |
| Rollback | `CHECKOUT_VERSION=1.0.0` → Deploy | `docker compose up -d checkout` | Los errores de la 1.1.0 paran |
| Notifier detenido | Detén `notifier` | `docker compose stop notifier` | *notifier: sin actividad* y *Cola de e-mails acumulada* |
| Base de datos caída | Detén `postgres` | `docker compose stop postgres` | **PostgreSQL: caído** (Disaster), *Servicio catalog/checkout* con errores |
| Toda la app detenida | Stop del servicio | `docker compose down` | *zabbix-bridge sin datos* (2 min) y *No spans received* (10 min) |

## 6. Variables de entorno

**Zabbix** (`zabbix-bridge`)

| Variable | Por defecto | Uso |
|---|---|---|
| `ZABBIX_URL` | — | URL del frontend de Zabbix (se le añade `/api_jsonrpc.php`). Sin ella no se integra |
| `ZABBIX_API_TOKEN` | — | Token de API de un Super admin |
| `ZABBIX_HOST` | `Fault Shop` | Host de la tienda (y prefijo del nombre del dashboard) |
| `ZABBIX_HOST_GROUP` | `Fault Shop` | Grupo de hosts |
| `ZABBIX_SEND_INTERVAL` | `10` | Segundos entre envíos |
| `ZABBIX_LLD_INTERVAL` | `600` | Segundos entre envíos de la LLD de servicios |
| `ZABBIX_TLS_INSECURE` | `0` | `1` acepta certificados autofirmados |
| `ZABBIX_TRANSPORT` | `api` | `api` = `history.push` por HTTPS · `sender` = protocolo Zabbix sender (TCP 10051) |
| `ZABBIX_SERVER` / `ZABBIX_PORT` | — / `10051` | Server o proxy para `ZABBIX_TRANSPORT=sender` |
| `ZABBIX_DASHBOARD` | `1` | Crear/actualizar el dashboard |
| `ZABBIX_APM_ENABLED` | `1` | Crear el host APM |
| `ZABBIX_APM_TEMPLATE` | `OpenTelemetry application by OTLP` | Template OTLP de Zabbix APM a enlazar |
| `ZABBIX_APM_HOST_NAME` | `apm-fault-shop` | Nombre del host APM |
| `ZABBIX_DRY_RUN` | `0` | `1` = no envía nada, solo lo escribe en el log |

**APM / OpenTelemetry** (todos los servicios instrumentados)

| Variable | Por defecto | Uso |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://host.docker.internal:4318` | Receptor OTLP (proxy APM de Zabbix `:4317` o Collector `:4318`) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | `grpc` para `:4317`, `http/protobuf` para `:4318` |
| `OTEL_EXPORTER_OTLP_HEADERS` | — | Cabeceras de autenticación |
| `OTEL_TRACES_EXPORTER` / `OTEL_LOGS_EXPORTER` / `OTEL_METRICS_EXPORTER` | `otlp` / `otlp` / `none` | Señales enviadas |
| `OTEL_SERVICE_NAMESPACE` | `fault-shop` | `service.namespace` (aplicación en APM) y `{$OTLP.NAMESPACE}` |
| `DEPLOY_ENV` | `demo` | `deployment.environment.name` |
| `RUM_OTLP_HTTP_ENDPOINT` | `http://host.docker.internal:4318` | Receptor OTLP/HTTP para spans del navegador |

**Tienda**

| Variable | Por defecto | Uso |
|---|---|---|
| `CHECKOUT_VERSION` | `1.0.0` | `1.1.0` despliega la release defectuosa |
| `SHOP_PORT` / `BRIDGE_PORT` | `18090` / `18091` | Solo Docker (`compose.local.yaml`) |

**Macros del template** (se ajustan en el host o el template): `{$FAULTSHOP.NODATA}=2m`, `{$FAULTSHOP.RT.MAX}=2000` (ms), `{$FAULTSHOP.QUEUE.MAX}=20`, `{$FAULTSHOP.PG.CONN.MAX}=80`, `{$FAULTSHOP.NOTIFIER.MAX}=60` (s), `{$FAULTSHOP.ORDERS.WINDOW}=3m`.

## 7. Alternativa: Zabbix por HTTP agent (pull)

Si prefieres que Zabbix consulte la tienda en vez de recibir datos:

1. Deja `ZABBIX_URL` vacío (el bridge no envía nada, solo sirve `/api/metrics`).
2. Expón el bridge: en Dokploy, un dominio para el servicio **`zabbix-bridge`** puerto **`8000`** (p. ej. `fshop-metrics.tu-dominio.com`, idealmente con Basic Auth o solo en red interna); en Docker, `BRIDGE_PORT`.
3. Importa [`zabbix/template_fault_shop_http.yaml`](zabbix/template_fault_shop_http.yaml) (*Data collection → Templates → Import*), crea un host enlazado a **Fault Shop by HTTP** y pon la macro `{$FAULTSHOP.URL}` (p. ej. `https://fshop-metrics.tu-dominio.com`).

Mismas claves, triggers y LLD que el modo push: un ítem HTTP agent `fshop.raw` cada `{$FAULTSHOP.INTERVAL}` y ítems dependientes con JSONPath. El dashboard en este modo se crea a mano o temporalmente con el modo push.

## 8. Solución de problemas

| Síntoma | Causa probable |
|---|---|
| Log `Provisión falló … HTTP 4xx/5xx` o `Not authorised` | URL sin acceso desde el servidor de Dokploy, token inválido o usuario sin rol Super admin. El bridge reintenta (10 s → 60 s) |
| `Zabbix x.y: se necesita 7.0 o superior` | `history.push` existe desde Zabbix 7.0. Para 6.x usa `ZABBIX_TRANSPORT=sender` + `ZABBIX_SERVER` e importa el template a mano |
| `certificate verify failed` | Certificado autofirmado: `ZABBIX_TLS_INSECURE=1` |
| Envío con rechazos durante el primer minuto | Normal: los ítems por servicio aparecen cuando Zabbix procesa la LLD. El bridge reenvía la LLD y el dashboard se completa solo |
| Host APM omitido | Tu Zabbix no tiene el template `OpenTelemetry application by OTLP` (Zabbix APM). Instálalo o cambia `ZABBIX_APM_TEMPLATE` |
| No llegan trazas | Revisa `OTEL_EXPORTER_OTLP_ENDPOINT`/`PROTOCOL` (4317 = grpc, 4318 = http/protobuf) y que el receptor sea alcanzable desde la red de Docker. Los logs de cada servicio muestran errores del exporter |
| `web` no arranca: `host not found in upstream` | `RUM_OTLP_HTTP_ENDPOINT` apunta a un nombre que no resuelve; corrígelo o déjalo en `http://host.docker.internal:4318` |
| `ZABBIX_TRANSPORT=sender` sin datos | Abre TCP 10051 hacia `ZABBIX_SERVER` |

## 9. Estructura y desarrollo

```
compose.yaml              servicios (Dokploy y Docker)
compose.local.yaml        puertos publicados para Docker sin Dokploy
.env.example              todas las variables
nginx/default.conf.template   nginx: /api, /otlp (RUM), /healthz y stub_status interno :8081
web/                      páginas de la tienda, panel de fallas, RUM (otel.js)
python/                   shop, checkout, notifier, bank, loadgen, faults
  zabbix_bridge.py        recolección + provisión + envío a Zabbix
  zabbix_api.py           cliente JSON-RPC (token), history.push, protocolo sender
  zabbix_dashboard.py     dashboard "<host> · Estado y APM"
  fshop_items.py          catálogo de ítems: fuente única para el bridge y los templates
node/                     catalog, payment
db/init.sql               esquema y productos
tools/gen_zabbix_template.py  genera zabbix/*.yaml desde fshop_items.py
zabbix/                   templates exportados (formato 7.0, importables en 7.0–8.0)
```

Si cambias ítems o triggers, edita `python/fshop_items.py` (o el generador) y regenera:

```sh
python3 tools/gen_zabbix_template.py
```

Probado: sintaxis de todos los servicios, consistencia de los templates (cada trigger referencia claves existentes) y el ciclo provisión → LLD → `history.push` → dashboard del bridge contra una API de Zabbix simulada. Probar en tu Zabbix real tras el primer despliegue.
