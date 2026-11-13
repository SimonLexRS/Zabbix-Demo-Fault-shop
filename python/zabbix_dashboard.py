"""Zabbix dashboard "<host> · Estado y APM", created or updated by zabbix_bridge.py (only the dashboard with that exact
name is touched). Two pages: the store (services, business, Redis, PostgreSQL, nginx) and APM + fault panel.

Per-service tiles need the items discovered by LLD: until they exist the tiles are left out and the bridge tries again.
"""
from fshop_items import FAULTS, SERVICES
from zabbix_api import Api

RED, ORANGE, YELLOW, GREEN, BLUE, CYAN, GRAY, PURPLE = 'E45959', 'FFA059', 'FFC859', '59DB8F', '7499FF', '3EE6FF', '97AAB3', 'B57BFF'


def dashboard_name(host: str) -> str:
	return f'{host} · Estado y APM'


def upsert_dashboard(api: Api, host: str, group: str, apm_host: str = '') -> dict:
	"""Returns {'id', 'action', 'name', 'complete'}; complete is False while LLD items are still missing."""
	hosts = api('host.get', {'filter': {'host': [host]}, 'output': ['hostid']})
	if not hosts:
		raise RuntimeError(f'no existe el host {host}')
	hostid = hosts[0]['hostid']

	apm = api('host.get', {'filter': {'host': [apm_host]}, 'output': ['hostid']}) if apm_host else []
	groups = api('hostgroup.get', {'filter': {'name': [group]}, 'output': ['groupid']})
	items = {i['key_']: i['itemid'] for i in api('item.get', {'hostids': [hostid], 'output': ['itemid', 'key_']})}
	missing = []
	refs = iter(range(10_000))

	def ref():
		n = next(refs)
		return f'FSH{chr(65 + n // 26 % 26)}{chr(65 + n % 26)}'

	def f(kind, name, value):
		return {'type': kind, 'name': name, 'value': str(value)}

	def thresholds(th):
		return [field for i, (value, color) in enumerate(th)
			for field in (f(1, f'thresholds.{i}.threshold', value), f(1, f'thresholds.{i}.color', color))]

	def tile(x, y, w, h, key, desc, dec=0, th=()):
		if key not in items:
			missing.append(key)
			return None
		return {'type': 'item', 'name': desc, 'x': x, 'y': y, 'width': w, 'height': h, 'view_mode': 0, 'fields': [
			{'type': 4, 'name': 'itemid.0', 'value': items[key]}, f(1, 'description', desc),
			f(0, 'show.0', 1), f(0, 'show.1', 2), f(0, 'show.2', 4), f(0, 'decimal_places', dec),
			f(0, 'desc_size', 14), f(0, 'value_size', 36), *thresholds(th)]}

	def graph(x, y, w, h, title, datasets, problems=False, graph_host=host):
		fields = []
		for i, ds in enumerate(datasets):
			fields += [f(1, f'ds.{i}.hosts.0', graph_host),
				*[f(1, f'ds.{i}.items.{k}', pattern) for k, pattern in enumerate(ds['items'])],
				f(1, f'ds.{i}.color', ds['color']), f(0, f'ds.{i}.type', 0), f(0, f'ds.{i}.width', 2),
				f(0, f'ds.{i}.fill', ds.get('fill', 1)), f(0, f'ds.{i}.axisy', ds.get('axis', 0)),
				f(0, f'ds.{i}.transparency', 3)]
		fields += [f(0, 'legend', 1), f(0, 'show_problems', 1 if problems else 0),
			*([f(1, 'problemhosts.0', graph_host)] if problems else []), f(1, 'reference', ref())]
		return {'type': 'svggraph', 'name': title, 'x': x, 'y': y, 'width': w, 'height': h, 'fields': fields}

	def problems(x, y, w, h, title, hostids, tags=()):
		fields = [*[{'type': 3, 'name': f'hostids.{i}', 'value': hid} for i, hid in enumerate(hostids)],
			f(0, 'show', 3), f(0, 'show_tags', 3), f(1, 'tag_priority', 'component,fault,servicio'),
			f(0, 'show_lines', 12), f(0, 'show_opdata', 1), f(0, 'highlight_row', 1)]
		for i, (tag, value, operator) in enumerate(tags):
			fields += [f(1, f'tags.{i}.tag', tag), f(0, f'tags.{i}.operator', operator), f(1, f'tags.{i}.value', value)]
		return {'type': 'problems', 'name': title, 'x': x, 'y': y, 'width': w, 'height': h,
			'fields': fields + [f(1, 'reference', ref())]}

	up = [[0, RED], [1, GREEN]]
	page1 = [
		*[tile(i * 12, 0, 12, 3, f'fshop.svc.up[{name}]', f'{name}', th=up) for i, name in enumerate(SERVICES)],
		tile(0, 3, 12, 3, 'fshop.orders.per_min', 'Pedidos por minuto', th=[[0, RED], [1, GREEN]]),
		tile(12, 3, 12, 3, 'fshop.orders.revenue_5m', 'Ventas últimos 5 min', dec=2),
		tile(24, 3, 12, 3, 'fshop.orders.avg_ticket_5m', 'Ticket medio', dec=2),
		tile(36, 3, 12, 3, 'fshop.redis.queue_len', 'Cola de e-mails', th=[[0, GREEN], [5, YELLOW], [20, RED]]),
		tile(48, 3, 12, 3, 'fshop.notifier.heartbeat_age', 'notifier: último ciclo (s)', th=[[0, GREEN], [30, YELLOW], [60, RED]]),
		tile(60, 3, 12, 3, 'fshop.fault.active_count', 'Faults activos', th=[[0, GREEN], [1, ORANGE]]),
		graph(0, 6, 36, 5, 'Tiempo de respuesta de los servicios (health)',
			[{'items': ['Servicio *: tiempo de respuesta'], 'color': CYAN, 'fill': 0}], problems=True),
		graph(36, 6, 36, 5, 'Pedidos por minuto y cola de e-mails', [
			{'items': ['Negocio: pedidos por minuto'], 'color': GREEN, 'fill': 2},
			{'items': ['Redis: pedidos en cola (orders)'], 'color': ORANGE, 'axis': 1, 'fill': 0}]),
		problems(0, 11, 48, 6, 'Problemas de la tienda', [hostid]),
		{'type': 'problemsbysv', 'name': 'Problemas por severidad', 'x': 48, 'y': 11, 'width': 24, 'height': 6,
			'fields': [*([{'type': 2, 'name': 'groupids.0', 'value': groups[0]['groupid']}] if groups else []),
				f(0, 'show_type', 1), f(0, 'layout', 1), f(1, 'reference', ref())]},
		graph(0, 17, 24, 5, 'nginx: peticiones por segundo', [{'items': ['nginx: peticiones por segundo'], 'color': BLUE}]),
		graph(24, 17, 24, 5, 'PostgreSQL', [
			{'items': ['PostgreSQL: conexiones'], 'color': PURPLE, 'fill': 0},
			{'items': ['PostgreSQL: tiempo de consulta del bridge'], 'color': YELLOW, 'axis': 1, 'fill': 0}]),
		graph(48, 17, 24, 5, 'Redis', [
			{'items': ['Redis: carritos abiertos'], 'color': GRAY, 'fill': 0},
			{'items': ['Redis: operaciones por segundo'], 'color': RED, 'axis': 1, 'fill': 0}]),
	]

	apm_ids = [apm[0]['hostid']] if apm else []
	page2 = [
		problems(0, 0, 36, 7, 'APM: problemas de trazas (OpenTelemetry)' if apm_ids else
			'APM: host OTLP no configurado (ZABBIX_APM_ENABLED)', apm_ids or [hostid],
			() if apm_ids else (('component', 'servicios', 1),)),
		problems(36, 0, 36, 7, 'Faults inyectados desde el panel', [hostid], (('component', 'faults', 1),)),
		graph(0, 7, 36, 5, 'Faults de latencia (ms)', [
			{'items': [f'Fault: {FAULTS[key][0]}' for key in ('db_slow_ms', 'bank_slow_ms', 'cpu_burn_ms', 'notifier_slow_ms')],
				'color': ORANGE, 'fill': 0}]),
		graph(36, 7, 36, 5, 'Carga simulada y pagos fallidos', [
			{'items': [f'Fault: {FAULTS["load_rps"][0]}'], 'color': BLUE, 'fill': 2},
			{'items': [f'Fault: {FAULTS["payment_fail_pct"][0]}'], 'color': RED, 'axis': 1, 'fill': 0}]),
		*[tile(i * 12, 12, 12, 3, f'fshop.svc.http_code[{name}]', f'HTTP {name}',
			th=[[0, RED], [200, GREEN], [300, YELLOW], [500, RED]]) for i, name in enumerate(SERVICES)],
	]

	name = dashboard_name(host)
	body = {'name': name, 'display_period': 60, 'auto_start': 0, 'pages': [
		{'name': 'Estado de la tienda', 'widgets': [w for w in page1 if w]},
		{'name': 'APM y faults', 'widgets': [w for w in page2 if w]},
	]}

	existing = api('dashboard.get', {'filter': {'name': [name]}, 'output': ['dashboardid']})
	if existing:
		api('dashboard.update', {'dashboardid': existing[0]['dashboardid'], **body})
		return {'id': existing[0]['dashboardid'], 'action': 'actualizado', 'name': name, 'complete': not missing}

	result = api('dashboard.create', body)
	return {'id': result['dashboardids'][0], 'action': 'creado', 'name': name, 'complete': not missing}
