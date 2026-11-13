# Fault Shop · Zabbix + APM (OpenTelemetry → ClickHouse)

[Español](README.md) — the Spanish README is the complete guide.

A small microservice store in Docker with a **fault panel** (`/faults.html`): switches that slow the database, fail payments or take a service down. It shows how each failure looks in **Zabbix** (items, triggers, dashboard) and in **Zabbix APM** (OpenTelemetry traces and logs stored in ClickHouse).

Only the Zabbix URL and an API token are needed: on start, the `zabbix-bridge` container imports the template `Fault Shop by Zabbix sender`, creates the host group, the store host, the APM host (first existing of `Generic OpenTelemetry by OTLP`, `OpenTelemetry application by OTLP`, namespace macros = `fault-shop`) and the dashboard `Fault Shop · Estado y APM`, then pushes values every 10 s with `history.push` (Zabbix 7.0+).

## Dokploy

1. Zabbix: *Users → API tokens* → token of a **Super admin** user of the Zabbix this shop reports to.
2. Dokploy: *Create Service → Compose*, Git `https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop.git`, branch `main`, compose path `./docker-compose.yml`.
3. *Environment*:
   ```env
   ZABBIX_URL=https://zabbix.example.com        # or http://zabbix-web:8080 on the Zabbix network
   ZABBIX_API_TOKEN=<token of THAT Zabbix>
   CONTROL_TOKEN=<administrator key of the fault panel>
   ZABBIX_NETWORK=<ZBX_SHARED_NETWORK of the same Zabbix stack>   # deploy the Zabbix stack first
   ZABBIX_CH_PASSWORD='<CLICKHOUSE_PASSWORD of the Zabbix stack>'
   ```
   All five are required (the deploy names the missing one). Only `otel-collector` joins the Zabbix network to send
   telemetry: every service sends OTLP to it and it relays to `zabbix-proxy-apm:4317`. No host ports.
   The fault panel asks for `CONTROL_TOKEN` once per browser; without it the switches can be seen but not changed.
4. *Domains*: service `web`, port `80`. Deploy.
5. Check the `zabbix-bridge` logs: `Provisión OK` then `Dashboard: … complete: True`.

## Business KPIs and access

The services send OpenTelemetry business metrics (`shop.orders`, `shop.revenue`, `shop.order.amount`, `shop.items.sold`, `shop.orders.failed`, `shop.payments`, `shop.cart.additions`, `shop.emails`). The bridge links the template **APM business metrics by ClickHouse HTTP** to `apm-fault-shop`; set `ZABBIX_CH_USER`/`ZABBIX_CH_PASSWORD` (ClickHouse as seen from the Zabbix server). Dashboard: `python3 scripts/zabbix-apm-app.py kpi-dashboard fault-shop` from [zabbix-apm-dashboard](https://github.com/SimonLexRS/zabbix-apm-dashboard).

Protect the domain with a Traefik `basicAuth` middleware (bcrypt hash, `removeHeader`, HSTS headers, rate limit) attached in *Domains → Middlewares*: see the Spanish README.

## Docker

```sh
cp .env.example .env    # set ZABBIX_URL, ZABBIX_API_TOKEN, CONTROL_TOKEN, ZABBIX_NETWORK, ZABBIX_CH_PASSWORD
docker compose -f docker-compose.yml -f compose.local.yaml up -d --build
```

Store on `:18090`, bridge metrics on `:18091/api/metrics`. All variables: [`.env.example`](.env.example). Pull alternative (HTTP agent): [`zabbix/template_fault_shop_http.yaml`](zabbix/template_fault_shop_http.yaml).
