# Fault Shop · Zabbix + APM (OpenTelemetry → ClickHouse)

[Español](README.md) — the Spanish README is the complete guide.

A small microservice store in Docker with a **fault panel** (`/faults.html`): switches that slow the database, fail payments or take a service down. It shows how each failure looks in **Zabbix** (items, triggers, dashboard) and in **Zabbix APM** (OpenTelemetry traces and logs stored in ClickHouse).

Only a Zabbix URL and an API token are needed: on start, the `zabbix-bridge` container imports the template `Fault Shop by Zabbix sender`, creates the host group, the store host, the APM host (`OpenTelemetry application by OTLP`, `{$OTLP.NAMESPACE}=fault-shop`) and the dashboard `Fault Shop · Estado y APM`, then pushes values every 10 s with `history.push` (Zabbix 7.0+).

## Dokploy

1. Zabbix: *Users → API tokens* → token of a **Super admin** user.
2. Dokploy: *Create Service → Compose*, Git `https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop.git`, branch `main`, compose path `./compose.yaml`.
3. *Environment*:
   ```env
   ZABBIX_URL=https://zabbix.example.com
   ZABBIX_API_TOKEN=<token>
   OTEL_EXPORTER_OTLP_ENDPOINT=http://<zabbix-apm-proxy-or-collector>:4317
   OTEL_EXPORTER_OTLP_PROTOCOL=grpc            # http/protobuf for a Collector on :4318
   RUM_OTLP_HTTP_ENDPOINT=http://<collector>:4318
   ```
4. *Domains*: service `web`, port `80`. Deploy.
5. Check the `zabbix-bridge` logs: `Provisión OK` then `Dashboard: … complete: True`.

## Docker

```sh
cp .env.example .env    # set ZABBIX_URL, ZABBIX_API_TOKEN, OTEL_*
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Store on `:18090`, bridge metrics on `:18091/api/metrics`. All variables: [`.env.example`](.env.example). Pull alternative (HTTP agent): [`zabbix/template_fault_shop_http.yaml`](zabbix/template_fault_shop_http.yaml).
