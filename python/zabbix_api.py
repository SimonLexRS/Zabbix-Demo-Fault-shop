"""Zabbix API (JSON-RPC, API token) and Zabbix sender protocol: what zabbix_bridge.py needs to provision Zabbix and
send it values. Idempotent: safe to run on every start.
"""
import json
import socket
import struct

import requests

SENDER_TEMPLATE = 'Fault Shop by Zabbix sender'

IMPORT_RULES = {
	'template_groups': {'createMissing': True, 'updateExisting': True},
	'templates': {'createMissing': True, 'updateExisting': True},
	'items': {'createMissing': True, 'updateExisting': True, 'deleteMissing': True},
	'triggers': {'createMissing': True, 'updateExisting': True, 'deleteMissing': True},
	'discoveryRules': {'createMissing': True, 'updateExisting': True, 'deleteMissing': True},
	'valueMaps': {'createMissing': True, 'updateExisting': True, 'deleteMissing': True},
}


class ZabbixError(Exception):
	pass


class Api:
	def __init__(self, url: str, token: str, verify_tls: bool = True, timeout: int = 20):
		base = url.rstrip('/')
		self.url = base if base.endswith('api_jsonrpc.php') else base + '/api_jsonrpc.php'
		self.token = token
		self.verify = verify_tls
		self.timeout = timeout
		self.http = requests.Session()

	def __call__(self, method: str, params=None, auth: bool = True):
		headers = {'Content-Type': 'application/json-rpc'}
		if auth:
			headers['Authorization'] = f'Bearer {self.token}'

		response = self.http.post(self.url, headers=headers, verify=self.verify, timeout=self.timeout,
			data=json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params if params is not None else {}, 'id': 1}))

		if not response.ok:
			raise ZabbixError(f'{method}: HTTP {response.status_code}')

		body = response.json()
		if 'error' in body:
			error = body['error']
			raise ZabbixError(f"{method}: {error.get('message', '')} {error.get('data', '')}".strip())

		return body['result']


def version_tuple(version: str) -> tuple:
	return tuple(int(part) for part in version.split('.')[:2])


def ensure_group(api: Api, name: str) -> str:
	found = api('hostgroup.get', {'filter': {'name': [name]}, 'output': ['groupid']})

	return found[0]['groupid'] if found else api('hostgroup.create', {'name': name})['groupids'][0]


def template_id(api: Api, name: str):
	found = api('template.get', {'filter': {'host': [name]}, 'output': ['templateid']})

	return found[0]['templateid'] if found else None


def upsert_host(api: Api, host: str, groupid: str, templateid: str, macros=(), tags=(), description='') -> tuple:
	"""Creates the host, or adds the group and template to the existing one (its other links are kept)."""
	found = api('host.get', {'filter': {'host': [host]}, 'output': ['hostid'],
		'selectParentTemplates': ['templateid'], 'selectHostGroups': ['groupid'], 'selectMacros': ['macro', 'value']})

	if not found:
		hostid = api('host.create', {'host': host, 'name': host, 'description': description,
			'groups': [{'groupid': groupid}], 'templates': [{'templateid': templateid}],
			'macros': list(macros), 'tags': list(tags)})['hostids'][0]
		return hostid, 'creado'

	current = found[0]
	templates = [{'templateid': t['templateid']} for t in current['parentTemplates']]
	if templateid not in [t['templateid'] for t in templates]:
		templates.append({'templateid': templateid})

	groups = [{'groupid': g['groupid']} for g in current['hostgroups']]
	if groupid not in [g['groupid'] for g in groups]:
		groups.append({'groupid': groupid})

	# Our macros win; macros someone added by hand stay.
	merged = {m['macro']: m['value'] for m in current.get('macros', [])}
	merged.update({m['macro']: m['value'] for m in macros})

	api('host.update', {'hostid': current['hostid'], 'templates': templates, 'groups': groups,
		'macros': [{'macro': key, 'value': value} for key, value in merged.items()]})

	return current['hostid'], 'actualizado'


def provision(api: Api, cfg: dict, template_source: str) -> dict:
	"""Template, group, store host and (optionally) the APM host. Returns what it did."""
	version = api('apiinfo.version', {}, auth=False)
	if version_tuple(version) < (7, 0):
		raise ZabbixError(f'Zabbix {version}: se necesita 7.0 o superior (history.push)')

	api('configuration.import', {'format': 'yaml', 'source': template_source, 'rules': IMPORT_RULES})
	templateid = template_id(api, SENDER_TEMPLATE)
	if not templateid:
		raise ZabbixError(f'no se encontró el template "{SENDER_TEMPLATE}" tras importarlo')

	groupid = ensure_group(api, cfg['host_group'])
	hostid, action = upsert_host(api, cfg['host'], groupid, templateid,
		tags=[{'tag': 'app', 'value': 'fault-shop'}],
		description='Fault Shop: tienda demo con panel de fallas. Datos de zabbix-bridge (history.push / Zabbix sender). '
			'https://github.com/SimonLexRS/Zabbix-Demo-Fault-shop')
	result = {'version': version, 'hostid': hostid, 'host': f'host {action}', 'groupid': groupid, 'apm': 'deshabilitado'}

	if cfg['apm_enabled']:
		apm_template = template_id(api, cfg['apm_template'])
		if not apm_template:
			result['apm'] = f'template "{cfg["apm_template"]}" no existe en este Zabbix: host APM omitido'
		else:
			try:
				apm_hostid, apm_action = upsert_host(api, cfg['apm_host'], groupid, apm_template,
					macros=[{'macro': '{$OTLP.NAMESPACE}', 'value': cfg['namespace']}],
					tags=[{'tag': 'app', 'value': 'fault-shop'}, {'tag': 'apm', 'value': 'otlp'}],
					description='Fault Shop en Zabbix APM: trazas OpenTelemetry (ClickHouse) del namespace '
						f'{cfg["namespace"]}.')
				result['apm'] = f'host APM {apm_action}'
				result['apm_hostid'] = apm_hostid
			except ZabbixError as error:
				result['apm'] = f'host APM no creado: {error}'

	return result


def push(api: Api, values: list) -> dict:
	"""values: [{'host', 'key', 'value', 'clock'}] -> history.push (Zabbix 7.0+), in batches of 500."""
	processed = failed = 0
	errors = {}

	for start in range(0, len(values), 500):
		batch = [{**v, 'value': v['value'] if isinstance(v['value'], str) else str(v['value'])}
			for v in values[start:start + 500]]
		result = api('history.push', batch)
		data = result.get('data', []) if isinstance(result, dict) else []

		if not data:
			processed += len(batch)
			continue

		for entry in data:
			if 'error' in entry:
				failed += 1
				errors[entry['error']] = errors.get(entry['error'], 0) + 1
			else:
				processed += 1

	return {'processed': processed, 'failed': failed, 'total': len(values), 'errors': errors}


def send(server: str, port: int, values: list, timeout: int = 10) -> dict:
	"""The Zabbix sender protocol (TCP 10051): header ZBXD\\x01 + length + JSON."""
	payload = json.dumps({'request': 'sender data', 'data': [
		{**v, 'value': v['value'] if isinstance(v['value'], str) else str(v['value'])} for v in values
	]}).encode()

	with socket.create_connection((server, port), timeout=timeout) as connection:
		connection.sendall(b'ZBXD\x01' + struct.pack('<II', len(payload), 0) + payload)
		chunks = []
		while True:
			chunk = connection.recv(65536)
			if not chunk:
				break
			chunks.append(chunk)

	answer = b''.join(chunks)
	if not answer.startswith(b'ZBXD'):
		raise ZabbixError('respuesta inválida del Zabbix server/proxy')

	body = json.loads(answer[13:].decode())
	info = body.get('info', '')
	# "processed: 10; failed: 0; total: 10; seconds spent: 0.000"
	numbers = dict(part.strip().split(': ')[:2] for part in info.split(';') if ': ' in part)

	return {'processed': int(numbers.get('processed', 0)), 'failed': int(numbers.get('failed', 0)),
		'total': len(values), 'errors': {info: 1} if numbers.get('failed', '0') != '0' else {}}
