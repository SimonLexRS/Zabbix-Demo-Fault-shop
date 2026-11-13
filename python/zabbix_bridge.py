"""zabbix-bridge: makes Fault Shop a Zabbix host with nothing to configure in Zabbix but an API token.

On start it imports the template, creates the host group, the store host, the APM host (OpenTelemetry template, if
present) and the dashboard, through the Zabbix API. Then, every ZABBIX_SEND_INTERVAL seconds, it measures the store
(health of each service, Redis, PostgreSQL, nginx, notifier, fault switches) and sends the values to Zabbix with
history.push (default, only HTTPS to the frontend) or the Zabbix sender protocol (TCP 10051).

It also serves the same data for the pull alternative (template "Fault Shop by HTTP"):
	GET /api/metrics   JSON read by the HTTP agent item
	GET /api/zabbix/status   what the provisioning and the last send did
	GET /healthz
Not instrumented with OpenTelemetry on purpose: its probes are not store traffic.
"""
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2
import redis
import requests

import faults
import zabbix_api
from fshop_items import DISCOVERY_KEY, SERVICES, discovery, flatten
from zabbix_dashboard import upsert_dashboard

logging.basicConfig(level=logging.INFO, format='%(asctime)s zabbix-bridge %(levelname)s %(message)s')
log = logging.getLogger('zabbix-bridge')


def env(name: str, default: str = '') -> str:
	return os.environ.get(name, default).strip()


def flag(name: str, default: str = '0') -> bool:
	return env(name, default).lower() in ('1', 'true', 'yes', 'on')


CFG = {
	'url': env('ZABBIX_URL'),
	'token': env('ZABBIX_API_TOKEN'),
	'host': env('ZABBIX_HOST', 'Fault Shop'),
	'host_group': env('ZABBIX_HOST_GROUP', 'Fault Shop'),
	'interval': max(5, int(env('ZABBIX_SEND_INTERVAL', '10'))),
	'lld_interval': max(60, int(env('ZABBIX_LLD_INTERVAL', '600'))),
	'transport': env('ZABBIX_TRANSPORT', 'api').lower(),
	'server': env('ZABBIX_SERVER'),
	'port': int(env('ZABBIX_PORT', '10051')),
	'verify_tls': not flag('ZABBIX_TLS_INSECURE'),
	'dashboard': flag('ZABBIX_DASHBOARD', '1'),
	'apm_enabled': flag('ZABBIX_APM_ENABLED', '1'),
	'apm_template': env('ZABBIX_APM_TEMPLATE', 'OpenTelemetry application by OTLP'),
	'apm_host': env('ZABBIX_APM_HOST_NAME', 'apm-fault-shop'),
	'namespace': env('OTEL_SERVICE_NAMESPACE', 'fault-shop'),
	'dry_run': flag('ZABBIX_DRY_RUN'),
	'template_file': env('ZABBIX_TEMPLATE_FILE', '/app/zabbix/template_fault_shop_sender.yaml'),
	'database_url': env('DATABASE_URL', 'postgresql://shop:shop@postgres/shop'),
	'redis_host': env('REDIS_HOST', 'redis'),
	'nginx_status': env('NGINX_STATUS_URL', 'http://web:8081/nginx_status'),
	'port_http': int(env('BRIDGE_PORT', '8000')),
}

STATUS = {'zabbix': 'deshabilitado (sin ZABBIX_URL / ZABBIX_API_TOKEN)', 'provision': None, 'last_send': None,
	'dashboard': None, 'errors': 0}
LATEST = {'metrics': {}}
http = requests.Session()
store = redis.Redis(host=CFG['redis_host'], socket_timeout=3, socket_connect_timeout=3, decode_responses=True)


# ---------------------------------------------------------------- measuring the store

def probe(url: str) -> dict:
	started = time.perf_counter()
	try:
		response = http.get(url, timeout=5)
		return {'up': int(response.status_code == 200), 'http_code': response.status_code,
			'rt_ms': round((time.perf_counter() - started) * 1000, 1)}
	except requests.RequestException:
		# Refused, timed out or closed without answer (the bank switched off): down, no HTTP code.
		return {'up': 0, 'http_code': 0, 'rt_ms': None}


def measure_redis() -> tuple:
	try:
		store.ping()
		info = store.info()
		carts = sum(1 for _ in store.scan_iter('cart:*', count=500))
		beat = store.get('notifier:heartbeat')
		values = {'up': 1, 'queue_len': store.llen('orders'), 'carts': carts, 'used_memory': info.get('used_memory'),
			'clients': info.get('connected_clients'), 'ops_per_sec': info.get('instantaneous_ops_per_sec')}
		# No stamp in 10 minutes (it expires): report 600 s, the trigger fires long before.
		notifier = {'heartbeat_age': max(0, int(time.time()) - int(beat)) if beat else 600}
		return values, notifier
	except redis.RedisError:
		return {'up': 0}, {}


_index_ready = {'done': False}


def measure_postgres() -> tuple:
	started = time.perf_counter()
	try:
		with psycopg2.connect(CFG['database_url'], connect_timeout=5) as connection, connection.cursor() as cursor:
			if not _index_ready['done']:
				# The store database has no index on the date of the orders; these counts run every few seconds.
				cursor.execute('CREATE INDEX IF NOT EXISTS orders_created_idx ON orders (created)')
				_index_ready['done'] = True
			cursor.execute('SELECT coalesce(max(id), 0) FROM orders')
			total = cursor.fetchone()[0]
			cursor.execute("""
				SELECT count(*) FILTER (WHERE created > now() - interval '1 minute'), count(*), coalesce(sum(total), 0)
				FROM orders WHERE created > now() - interval '5 minutes'""")
			per_min, last5m, revenue = cursor.fetchone()
			cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()')
			connections = cursor.fetchone()[0]
			cursor.execute('SELECT pg_database_size(current_database())')
			size = cursor.fetchone()[0]
		connection.close()
	except psycopg2.Error:
		return {'up': 0}, {}

	revenue = float(revenue)
	pg = {'up': 1, 'rt_ms': round((time.perf_counter() - started) * 1000, 1), 'connections': connections, 'db_size': size}
	orders = {'total': total, 'per_min': per_min, 'last5m': last5m, 'revenue_5m': round(revenue, 2),
		'avg_ticket_5m': round(revenue / last5m, 2) if last5m else 0}

	return pg, orders


def measure_nginx() -> dict:
	try:
		text = http.get(CFG['nginx_status'], timeout=5).text
		active = int(re.search(r'Active connections:\s+(\d+)', text).group(1))
		accepts, handled, total = (int(n) for n in re.search(r'\n\s*(\d+)\s+(\d+)\s+(\d+)', text).groups())
		reading, writing, waiting = (int(n) for n in
			re.search(r'Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)', text).groups())
		return {'active': active, 'requests': total, 'reading': reading, 'writing': writing, 'waiting': waiting}
	except (requests.RequestException, AttributeError, ValueError):
		return {}


def measure_faults() -> dict:
	switches = faults.get()
	defaults = faults.defaults()
	values = {key: int(value) for key, value in switches.items() if key != 'down' and key in defaults}
	down = sorted(switches.get('down', []))
	values['down'] = ','.join(down) if down else 'ninguno'
	values['down_count'] = len(down)
	values['active_count'] = sum(1 for key, value in switches.items()
		if key not in ('load_rps', 'down') and key in defaults and value != defaults[key]) + (1 if down else 0)

	return values


def collect() -> dict:
	services = {}
	threads = [threading.Thread(target=lambda n=name, u=url: services.__setitem__(n, probe(u)))
		for name, url in SERVICES.items()]
	for thread in threads:
		thread.start()

	redis_values, notifier = measure_redis()
	pg, orders = measure_postgres()
	nginx = measure_nginx()

	for thread in threads:
		thread.join()

	return {
		'timestamp': int(time.time()),
		'bridge': {'heartbeat': int(time.time())},
		'services': {name: services[name] for name in SERVICES},
		'redis': redis_values, 'postgres': pg, 'orders': orders, 'nginx': nginx, 'notifier': notifier,
		'faults': measure_faults(),
		'discovery': {'services': discovery()},
	}


# ---------------------------------------------------------------- sending to Zabbix

def deliver(api, values: list) -> dict:
	if CFG['dry_run']:
		log.info('DRY RUN: %s', json.dumps(values, ensure_ascii=False))
		return {'processed': len(values), 'failed': 0, 'total': len(values), 'errors': {}}

	if CFG['transport'] == 'sender':
		return zabbix_api.send(CFG['server'], CFG['port'], values)

	return zabbix_api.push(api, values)


def run() -> None:
	can_provision = bool(CFG['url'] and CFG['token']) and not CFG['dry_run']
	api = zabbix_api.Api(CFG['url'], CFG['token'], CFG['verify_tls']) if CFG['url'] else None

	if CFG['dry_run']:
		enabled = True
	elif CFG['transport'] == 'sender':
		enabled = bool(CFG['server'])
		if not enabled:
			log.warning('ZABBIX_TRANSPORT=sender sin ZABBIX_SERVER: no se envía nada')
	else:
		enabled = can_provision

	if not CFG['verify_tls']:
		requests.packages.urllib3.disable_warnings()

	if enabled:
		STATUS['zabbix'] = 'dry-run' if CFG['dry_run'] else f"{CFG['transport']} -> {CFG['server'] if CFG['transport'] == 'sender' else CFG['url']}"
	log.info('Zabbix: %s; host "%s"; cada %s s', STATUS['zabbix'], CFG['host'], CFG['interval'])

	provisioned = not can_provision  # nothing to provision: send straight away (template imported by hand)
	backoff, next_provision = 10, 0.0
	lld_due, lld_sent_at = 0.0, 0.0
	dashboard_done, next_dashboard = not (can_provision and CFG['dashboard']), 0.0
	rejected_all = 0

	while True:
		started = time.monotonic()
		metrics = collect()
		LATEST['metrics'] = metrics

		if enabled:
			now = time.monotonic()

			if not provisioned and now >= next_provision:
				try:
					template = Path(CFG['template_file']).read_text(encoding='utf-8')
					STATUS['provision'] = zabbix_api.provision(api, CFG, template)
					log.info('Provisión OK: %s', STATUS['provision'])
					provisioned, backoff, lld_due = True, 10, 0.0
				except (zabbix_api.ZabbixError, requests.RequestException, OSError) as error:
					STATUS['provision'] = f'error: {error}'
					log.warning('Provisión falló (reintento en %s s): %s', backoff, error)
					next_provision = now + backoff
					backoff = min(backoff * 2, 60)

			if provisioned:
				clock = metrics['timestamp']
				host = CFG['host']

				try:
					if now >= lld_due:
						deliver(api, [{'host': host, 'key': DISCOVERY_KEY, 'value': json.dumps(discovery()), 'clock': clock}])
						lld_sent_at, lld_due = now, now + CFG['lld_interval']

					values = [{'host': host, 'key': key, 'value': value, 'clock': clock} for key, value in flatten(metrics)]
					result = deliver(api, values)
					STATUS['last_send'] = {'at': clock, **result}

					if result['failed']:
						# Usually the LLD items do not exist yet (first minute): send discovery again soon.
						log.info('Envío: %s/%s aceptados; rechazos: %s', result['processed'], result['total'], result['errors'])
						lld_due = min(lld_due, lld_sent_at + 60)

					# Every value refused three times in a row: host or template deleted by hand. Provision again.
					rejected_all = rejected_all + 1 if result['total'] and result['failed'] == result['total'] else 0
					if can_provision and rejected_all >= 3:
						log.warning('Zabbix rechaza todos los valores: se vuelve a provisionar')
						provisioned, next_provision, rejected_all = False, now, 0
						dashboard_done = not CFG['dashboard']
				except (zabbix_api.ZabbixError, requests.RequestException, OSError) as error:
					STATUS['errors'] += 1
					STATUS['last_send'] = {'at': clock, 'error': str(error)}
					log.warning('Envío falló: %s', error)

				# The dashboard needs the discovered per-service items: wait a little after the discovery.
				if not dashboard_done and now >= next_dashboard and now - lld_sent_at > 30:
					try:
						STATUS['dashboard'] = upsert_dashboard(api, CFG['host'], CFG['host_group'],
							CFG['apm_host'] if CFG['apm_enabled'] else '')
						dashboard_done = STATUS['dashboard']['complete']
						log.info('Dashboard: %s', STATUS['dashboard'])
					except (zabbix_api.ZabbixError, requests.RequestException, RuntimeError) as error:
						STATUS['dashboard'] = f'error: {error}'
						log.warning('Dashboard falló: %s', error)
					next_dashboard = now + 60

		time.sleep(max(1.0, CFG['interval'] - (time.monotonic() - started)))


# ---------------------------------------------------------------- HTTP: pull mode and status

class Handler(BaseHTTPRequestHandler):
	def do_GET(self):
		routes = {
			'/healthz': lambda: {'ok': True, 'service': 'zabbix-bridge'},
			'/api/metrics': lambda: LATEST['metrics'],
			'/api/zabbix/status': lambda: {**STATUS, 'host': CFG['host'], 'transport': CFG['transport'],
				'interval': CFG['interval']},
		}
		route = routes.get(self.path.split('?')[0])
		body = json.dumps(route() if route else {'error': 'not found'}, ensure_ascii=False, default=str).encode()

		self.send_response(200 if route else 404)
		self.send_header('Content-Type', 'application/json; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *args):
		pass


if __name__ == '__main__':
	threading.Thread(target=ThreadingHTTPServer(('', CFG['port_http']), Handler).serve_forever, daemon=True).start()
	run()
