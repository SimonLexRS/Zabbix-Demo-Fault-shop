"""What Zabbix gets from the store: one list shared by zabbix_bridge.py (values it pushes) and
tools/gen_zabbix_template.py (items, triggers and LLD of the templates). Change it here, then regenerate the templates.

Each item: key, name, path in the JSON of /api/metrics, value type, units, and optional preprocessing, value map, tags.
"""

# HTTP services probed by the bridge: name -> health URL inside the compose network.
SERVICES = {
	'web': 'http://web/healthz',
	'shop': 'http://shop:8000/health',
	'catalog': 'http://catalog:8000/health',
	'checkout': 'http://checkout:8000/health',
	'payment': 'http://payment:8000/health',
	'bank': 'http://bank:8000/health',
}

SERVICE_ROLES = {
	'web': 'nginx: store pages, /api and /otlp',
	'shop': 'API of the store (Python)',
	'catalog': 'Products and stock (Node.js + PostgreSQL)',
	'checkout': 'Orders (Python)',
	'payment': 'Card charges (Node.js)',
	'bank': 'Card issuer, outside service',
}

# Fault switches (python/faults.py) sent as numbers; booleans as 0/1. "down" is sent apart.
FAULTS = {
	'db_slow_ms': ('Base de datos lenta', 'ms'),
	'n_plus_one': ('Consultas N+1', ''),
	'poison_product': ('Producto que rompe el código', ''),
	'out_of_stock': ('Sin stock', ''),
	'payment_fail_pct': ('Pagos fallidos', '%'),
	'bank_slow_ms': ('Banco lento', 'ms'),
	'cpu_burn_ms': ('Checkout con CPU', 'ms'),
	'notifier_slow_ms': ('Cola de e-mail lenta', 'ms'),
	'notifier_fail': ('E-mails fallan', ''),
	'js_error': ('Error JavaScript en "Agregar al carrito"', ''),
	'load_rps': ('Clientes simulados por segundo', 'rps'),
}
BOOLEAN_FAULTS = {'n_plus_one', 'poison_product', 'out_of_stock', 'notifier_fail', 'js_error'}


def item(key, name, path, value_type='FLOAT', units='', component='general', valuemap=None, preprocessing=(),
		description=''):
	return {
		'key': key, 'name': name, 'path': path, 'value_type': value_type, 'units': units, 'component': component,
		'valuemap': valuemap, 'preprocessing': list(preprocessing), 'description': description
	}


ITEMS = [
	item('fshop.bridge.heartbeat', 'Fault Shop: latido del bridge', ('bridge', 'heartbeat'), 'UNSIGNED', 'unixtime',
		description='Hora de la última recolección de zabbix-bridge. Sin datos = la app no está enviando.'),

	# Redis: carts and the "orders" queue
	item('fshop.redis.up', 'Redis: disponible', ('redis', 'up'), 'UNSIGNED', component='redis', valuemap='Fault Shop: estado'),
	item('fshop.redis.queue_len', 'Redis: pedidos en cola (orders)', ('redis', 'queue_len'), 'UNSIGNED', component='redis'),
	item('fshop.redis.carts', 'Redis: carritos abiertos', ('redis', 'carts'), 'UNSIGNED', component='redis'),
	item('fshop.redis.used_memory', 'Redis: memoria usada', ('redis', 'used_memory'), 'UNSIGNED', 'B', component='redis'),
	item('fshop.redis.clients', 'Redis: clientes conectados', ('redis', 'clients'), 'UNSIGNED', component='redis'),
	item('fshop.redis.ops_per_sec', 'Redis: operaciones por segundo', ('redis', 'ops_per_sec'), units='ops', component='redis'),

	# PostgreSQL
	item('fshop.pg.up', 'PostgreSQL: disponible', ('postgres', 'up'), 'UNSIGNED', component='postgres', valuemap='Fault Shop: estado'),
	item('fshop.pg.rt_ms', 'PostgreSQL: tiempo de consulta del bridge', ('postgres', 'rt_ms'), units='ms', component='postgres'),
	item('fshop.pg.connections', 'PostgreSQL: conexiones', ('postgres', 'connections'), 'UNSIGNED', component='postgres'),
	item('fshop.pg.db_size', 'PostgreSQL: tamaño de la base', ('postgres', 'db_size'), 'UNSIGNED', 'B', component='postgres'),

	# Business
	item('fshop.orders.total', 'Negocio: pedidos totales', ('orders', 'total'), 'UNSIGNED', component='negocio'),
	item('fshop.orders.per_min', 'Negocio: pedidos por minuto', ('orders', 'per_min'), 'UNSIGNED', 'opm', component='negocio'),
	item('fshop.orders.last5m', 'Negocio: pedidos últimos 5 min', ('orders', 'last5m'), 'UNSIGNED', component='negocio'),
	item('fshop.orders.revenue_5m', 'Negocio: ventas últimos 5 min', ('orders', 'revenue_5m'), units='USD', component='negocio'),
	item('fshop.orders.avg_ticket_5m', 'Negocio: ticket medio últimos 5 min', ('orders', 'avg_ticket_5m'), units='USD', component='negocio'),

	# nginx (stub_status on the internal port 8081)
	item('fshop.nginx.active', 'nginx: conexiones activas', ('nginx', 'active'), 'UNSIGNED', component='nginx'),
	item('fshop.nginx.requests', 'nginx: peticiones por segundo', ('nginx', 'requests'), units='rps', component='nginx',
		preprocessing=[('CHANGE_PER_SECOND', '')]),
	item('fshop.nginx.reading', 'nginx: conexiones leyendo', ('nginx', 'reading'), 'UNSIGNED', component='nginx'),
	item('fshop.nginx.writing', 'nginx: conexiones escribiendo', ('nginx', 'writing'), 'UNSIGNED', component='nginx'),
	item('fshop.nginx.waiting', 'nginx: conexiones en espera', ('nginx', 'waiting'), 'UNSIGNED', component='nginx'),

	# notifier: a worker with no port; it stamps Redis on every loop
	item('fshop.notifier.heartbeat_age', 'notifier: segundos desde su último ciclo', ('notifier', 'heartbeat_age'),
		units='s', component='notifier'),

	# Fault panel
	*[item(f'fshop.fault[{key}]', f'Fault: {label}', ('faults', key), 'UNSIGNED', units, component='faults',
		valuemap='Fault Shop: sí/no' if key in BOOLEAN_FAULTS else None) for key, (label, units) in FAULTS.items()],
	item('fshop.fault.down', 'Fault: servicios apagados desde el panel', ('faults', 'down'), 'CHAR', component='faults'),
	item('fshop.fault.down_count', 'Fault: cantidad de servicios apagados', ('faults', 'down_count'), 'UNSIGNED', component='faults'),
	item('fshop.fault.active_count', 'Fault: switches activos', ('faults', 'active_count'), 'UNSIGNED', component='faults'),
]

# Per-service items, discovered with fshop.svc.discovery ({#SVC}); path ('services', '{#SVC}', field).
SERVICE_PROTOTYPES = [
	item('fshop.svc.up[{#SVC}]', 'Servicio {#SVC}: disponible', 'up', 'UNSIGNED', component='servicios', valuemap='Fault Shop: estado'),
	item('fshop.svc.rt_ms[{#SVC}]', 'Servicio {#SVC}: tiempo de respuesta', 'rt_ms', units='ms', component='servicios'),
	item('fshop.svc.http_code[{#SVC}]', 'Servicio {#SVC}: código HTTP', 'http_code', 'UNSIGNED', component='servicios'),
]

DISCOVERY_KEY = 'fshop.svc.discovery'


def flatten(metrics: dict) -> list:
	"""(key, value) pairs of a metrics document, skipping what could not be measured (None)."""
	pairs = []

	for definition in ITEMS:
		value = metrics
		for part in definition['path']:
			value = value.get(part) if isinstance(value, dict) else None
		if value is not None:
			pairs.append((definition['key'], value))

	for name, values in metrics.get('services', {}).items():
		for prototype in SERVICE_PROTOTYPES:
			value = values.get(prototype['path'])
			if value is not None:
				pairs.append((prototype['key'].replace('{#SVC}', name), value))

	return pairs


def discovery() -> list:
	return [{'{#SVC}': name, '{#ROLE}': SERVICE_ROLES[name]} for name in SERVICES]
