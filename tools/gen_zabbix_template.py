#!/usr/bin/env python3
"""Generates the Zabbix templates of Fault Shop from python/fshop_items.py (one list for the bridge and Zabbix):

	zabbix/template_fault_shop_sender.yaml   "Fault Shop by Zabbix sender": trapper items; zabbix-bridge imports it
	zabbix/template_fault_shop_http.yaml     "Fault Shop by HTTP": HTTP agent on /api/metrics + dependent items

	python3 tools/gen_zabbix_template.py

Export format 7.0: importable in Zabbix 7.0, 7.2, 7.4 and 8.0. UUIDs derive from stable seeds, so re-importing updates
the same objects instead of duplicating them.
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'python'))

from fshop_items import BOOLEAN_FAULTS, DISCOVERY_KEY, FAULTS, ITEMS, SERVICE_PROTOTYPES  # noqa: E402

GROUP = 'Templates/Applications'
REPO = 'https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop'


# ---------------------------------------------------------------- tiny YAML emitter (every scalar single-quoted)

def quote(value) -> str:
	return "'" + str(value).replace("'", "''") + "'"


def emit(value, indent=0) -> list:
	pad = '  ' * indent
	lines = []

	if isinstance(value, dict):
		for key, child in value.items():
			if isinstance(child, (dict, list)):
				if not child:
					continue
				lines.append(f'{pad}{key}:')
				lines += emit(child, indent + 1)
			elif child is not None:
				lines.append(f'{pad}{key}: {quote(child)}')
	else:
		for child in value:
			if isinstance(child, dict):
				inner = emit(child, indent + 1)
				lines.append(f'{pad}- ' + inner[0].lstrip())
				lines += inner[1:]
			else:
				lines.append(f'{pad}- {quote(child)}')

	return lines


# ---------------------------------------------------------------- template

def build(mode: str) -> dict:
	http = mode == 'http'
	template = 'Fault Shop by HTTP' if http else 'Fault Shop by Zabbix sender'
	T = f'/{template}/'

	def uuid(seed: str) -> str:
		digest = list(hashlib.md5(f'fault-shop:{mode}:{seed}'.encode()).hexdigest())
		digest[12] = '4'
		digest[16] = '89ab'[int(digest[16], 16) % 4]
		return ''.join(digest)

	def jsonpath(path) -> str:
		head, *rest = path
		return f'$.{head}' + ''.join(f"['{part}']" for part in rest)

	def preprocessing(definition, path):
		steps = []
		if http:
			steps.append({'type': 'JSONPATH', 'parameters': [path], 'error_handler': 'DISCARD_VALUE'})
		for kind, parameter in definition['preprocessing']:
			steps.append({'type': kind, 'parameters': [parameter]})
		return steps

	def make_item(definition, path, triggers=(), prototype=False):
		result = {
			'uuid': uuid(definition['key']),
			'name': definition['name'],
			'type': 'DEPENDENT' if http else 'TRAP',
			'key': definition['key'],
			'delay': '0' if http else None,
			'value_type': definition['value_type'],
			'trends': '0' if definition['value_type'] in ('CHAR', 'TEXT') else None,
			'units': definition['units'] or None,
			'description': definition['description'] or None,
			'valuemap': {'name': definition['valuemap']} if definition['valuemap'] else None,
			'preprocessing': preprocessing(definition, path),
			'master_item': {'key': 'fshop.raw'} if http else None,
			'tags': [{'tag': 'component', 'value': definition['component']}] +
				([{'tag': 'servicio', 'value': '{#SVC}'}] if prototype else []),
		}
		result['trigger_prototypes' if prototype else 'triggers'] = list(triggers)
		return result

	def trigger(seed, expression, name, priority, description='', tags=(), manual_close=False, dependencies=(),
			opdata=None):
		return {
			'uuid': uuid('trigger:' + seed),
			'expression': expression,
			'name': name,
			'opdata': opdata,
			'priority': priority,
			'description': description or None,
			'manual_close': 'YES' if manual_close else None,
			'dependencies': list(dependencies),
			'tags': list(tags),
		}

	nodata_name = 'Fault Shop: zabbix-bridge sin datos'
	nodata_expr = f'nodata({T}fshop.bridge.heartbeat,{{$FAULTSHOP.NODATA}})=1'
	depends_bridge = [{'name': nodata_name, 'expression': nodata_expr}]

	triggers = {
		'fshop.bridge.heartbeat': [trigger('nodata', nodata_expr, nodata_name, 'HIGH',
			'El bridge dejó de enviar (contenedor detenido, sin red hacia Zabbix o token inválido). '
			'Los demás triggers de este host dependen de este.', [{'tag': 'component', 'value': 'general'}])],
		'fshop.redis.up': [trigger('redis.down', f'max({T}fshop.redis.up,#2)=0', 'Redis: caído', 'DISASTER',
			'Sin Redis no hay carritos ni cola de e-mails.', [{'tag': 'component', 'value': 'redis'}],
			dependencies=depends_bridge)],
		'fshop.redis.queue_len': [trigger('queue', f'min({T}fshop.redis.queue_len,2m)>{{$FAULTSHOP.QUEUE.MAX}}',
			'Cola de e-mails acumulada (> {$FAULTSHOP.QUEUE.MAX} pedidos)', 'AVERAGE',
			'notifier no procesa al ritmo de los pedidos: fault "Cola de e-mail lenta" o notifier caído.',
			[{'tag': 'component', 'value': 'notifier'}], opdata='En cola: {ITEM.LASTVALUE1}', dependencies=depends_bridge)],
		'fshop.pg.up': [trigger('pg.down', f'max({T}fshop.pg.up,#2)=0', 'PostgreSQL: caído', 'DISASTER',
			tags=[{'tag': 'component', 'value': 'postgres'}], dependencies=depends_bridge)],
		'fshop.pg.connections': [trigger('pg.conn', f'min({T}fshop.pg.connections,5m)>{{$FAULTSHOP.PG.CONN.MAX}}',
			'PostgreSQL: demasiadas conexiones (> {$FAULTSHOP.PG.CONN.MAX})', 'WARNING',
			tags=[{'tag': 'component', 'value': 'postgres'}], opdata='Conexiones: {ITEM.LASTVALUE1}')],
		'fshop.notifier.heartbeat_age': [trigger('notifier', f'min({T}fshop.notifier.heartbeat_age,1m)>{{$FAULTSHOP.NOTIFIER.MAX}}',
			'notifier: sin actividad (> {$FAULTSHOP.NOTIFIER.MAX} s)', 'HIGH',
			'El worker no completa su ciclo: contenedor detenido o bloqueado en un e-mail lento.',
			[{'tag': 'component', 'value': 'notifier'}], opdata='Último ciclo hace {ITEM.LASTVALUE1}',
			dependencies=depends_bridge)],
		'fshop.fault.down_count': [trigger('fault.down', f'last({T}fshop.fault.down_count)>0',
			'Fault: servicios apagados desde el panel', 'WARNING',
			'El panel de fallas tiene servicios en "down" (503 o conexión cerrada).',
			[{'tag': 'component', 'value': 'faults'}, {'tag': 'fault', 'value': 'down'}],
			opdata='Apagados: {ITEM.LASTVALUE1}')],
	}

	for key, (label, units) in FAULTS.items():
		if key == 'load_rps':
			continue
		triggers[f'fshop.fault[{key}]'] = [trigger(f'fault.{key}', f'last({T}fshop.fault[{key}])>0',
			f'Fault inyectado: {label}', 'INFO',
			'Switch activo en el panel de fallas (/faults.html). Se resuelve al apagarlo o con "Volver a la normalidad".',
			[{'tag': 'component', 'value': 'faults'}, {'tag': 'fault', 'value': key}],
			opdata=None if key in BOOLEAN_FAULTS else 'Valor: {ITEM.LASTVALUE1}')]

	items = []
	if http:
		items.append({
			'uuid': uuid('fshop.raw'), 'name': 'Fault Shop: datos crudos (/api/metrics)', 'type': 'HTTP_AGENT',
			'key': 'fshop.raw', 'delay': '{$FAULTSHOP.INTERVAL}', 'history': '0', 'value_type': 'TEXT', 'trends': '0',
			'url': '{$FAULTSHOP.URL}/api/metrics', 'timeout': '10s',
			'tags': [{'tag': 'component', 'value': 'raw'}],
		})
	for definition in ITEMS:
		items.append(make_item(definition, jsonpath(definition['path']), triggers.get(definition['key'], ())))

	svc_name = 'Servicio {#SVC}: '
	prototype_triggers = {
		'fshop.svc.up[{#SVC}]': [trigger('svc.down', f'max({T}fshop.svc.up[{{#SVC}}],#2)=0', svc_name + 'caído', 'HIGH',
			'{#ROLE}. El health check no responde 200: fault "down" del panel, contenedor detenido o error.',
			[{'tag': 'component', 'value': 'servicios'}, {'tag': 'servicio', 'value': '{#SVC}'}],
			dependencies=depends_bridge)],
		'fshop.svc.rt_ms[{#SVC}]': [trigger('svc.slow', f'min({T}fshop.svc.rt_ms[{{#SVC}}],1m)>{{$FAULTSHOP.RT.MAX}}',
			svc_name + 'health check lento (> {$FAULTSHOP.RT.MAX} ms)', 'WARNING',
			tags=[{'tag': 'component', 'value': 'servicios'}, {'tag': 'servicio', 'value': '{#SVC}'}],
			opdata='Respuesta: {ITEM.LASTVALUE1}')],
	}
	prototypes = [make_item(p, f"$.services['{{#SVC}}'].{p['path']}", prototype_triggers.get(p['key'], ()), prototype=True)
		for p in SERVICE_PROTOTYPES]

	discovery_rule = {
		'uuid': uuid(DISCOVERY_KEY),
		'name': 'Servicios de la tienda',
		'type': 'DEPENDENT' if http else 'TRAP',
		'key': DISCOVERY_KEY,
		'delay': '0' if http else None,
		'description': 'Servicios HTTP de Fault Shop con health check (web, shop, catalog, checkout, payment, bank).',
		'item_prototypes': prototypes,
		'master_item': {'key': 'fshop.raw'} if http else None,
		'preprocessing': [{'type': 'JSONPATH', 'parameters': ['$.discovery.services']}] if http else [],
	}

	macros = [
		('{$FAULTSHOP.NODATA}', '2m', 'Tiempo sin datos del bridge antes de alertar.'),
		('{$FAULTSHOP.RT.MAX}', '2000', 'Tiempo máximo de respuesta del health check (ms).'),
		('{$FAULTSHOP.QUEUE.MAX}', '20', 'Pedidos en cola de e-mails tolerados durante 2 min.'),
		('{$FAULTSHOP.PG.CONN.MAX}', '80', 'Conexiones máximas a PostgreSQL.'),
		('{$FAULTSHOP.NOTIFIER.MAX}', '60', 'Segundos sin ciclo del worker notifier.'),
		('{$FAULTSHOP.ORDERS.WINDOW}', '3m', 'Ventana sin pedidos (con carga simulada activa) antes de alertar.'),
	]
	if not http:
		# Zabbix 7/8 trapper items accept values only from {$TRAPPER.ALLOWED_HOSTS} (global default 127.0.0.1,::1).
		# With history.push the address checked is the one of the Zabbix frontend (a Docker private IP).
		macros.insert(0, ('{$TRAPPER.ALLOWED_HOSTS}', '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1,::1',
			'Orígenes permitidos para los ítems trapper (frontend con history.push o zabbix_sender). Redes privadas.'))
	if http:
		macros += [('{$FAULTSHOP.URL}', 'http://zabbix-bridge:8000', 'URL base del zabbix-bridge (sin / final).'),
			('{$FAULTSHOP.INTERVAL}', '30s', 'Intervalo del HTTP agent.')]

	export = {
		'zabbix_export': {
			'version': '7.0',
			'template_groups': [{'uuid': uuid('group'), 'name': GROUP}],
			'templates': [{
				'uuid': uuid('template'),
				'template': template,
				'name': template,
				'description': ('Fault Shop: tienda demo de microservicios con panel de fallas. '
					+ ('Datos por HTTP agent desde {$FAULTSHOP.URL}/api/metrics.' if http else
						'Datos enviados por zabbix-bridge (history.push o Zabbix sender, ítems trapper).')
					+ f' Las trazas OpenTelemetry van al host APM (template OpenTelemetry application by OTLP). {REPO}'),
				'groups': [{'name': GROUP}],
				'items': items,
				'discovery_rules': [discovery_rule],
				'tags': [{'tag': 'class', 'value': 'application'}, {'tag': 'target', 'value': 'fault-shop'}],
				'macros': [{'macro': m, 'value': v, 'description': d} for m, v, d in macros],
				'valuemaps': [
					{'uuid': uuid('vm.estado'), 'name': 'Fault Shop: estado',
						'mappings': [{'value': '0', 'newvalue': 'Caído'}, {'value': '1', 'newvalue': 'Disponible'}]},
					{'uuid': uuid('vm.sino'), 'name': 'Fault Shop: sí/no',
						'mappings': [{'value': '0', 'newvalue': 'No'}, {'value': '1', 'newvalue': 'Sí'}]},
				],
			}],
			# Triggers over more than one item live outside the template in the export format.
			'triggers': [trigger('no-orders',
				f'max({T}fshop.orders.per_min,{{$FAULTSHOP.ORDERS.WINDOW}})=0 and last({T}fshop.fault[load_rps])>0',
				'Fault Shop: sin pedidos con clientes simulados activos', 'AVERAGE',
				'loadgen genera tráfico pero ningún pedido llega a PostgreSQL: la compra está rota en algún punto '
				'(pagos, stock, servicio caído). Ver trazas en APM.',
				[{'tag': 'component', 'value': 'negocio'}], dependencies=depends_bridge)],
		}
	}
	return export


def main() -> None:
	out = ROOT / 'zabbix'
	out.mkdir(exist_ok=True)
	for mode, name in (('sender', 'template_fault_shop_sender.yaml'), ('http', 'template_fault_shop_http.yaml')):
		text = '\n'.join(emit(build(mode))) + '\n'
		(out / name).write_text(text, encoding='utf-8')
		print(f'{out / name}: {text.count(chr(10))} líneas')


if __name__ == '__main__':
	main()
