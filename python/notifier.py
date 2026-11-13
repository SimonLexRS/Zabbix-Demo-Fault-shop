"""notifier: takes each order from the Redis queue "orders" and sends its confirmation e-mail (simulated)."""
import json
import logging
import time

import redis
from opentelemetry import metrics, propagate, trace
from opentelemetry.trace import SpanKind

import faults

log = logging.getLogger('notifier')
logging.basicConfig(level=logging.INFO)
logging.getLogger().setLevel(logging.INFO)
tracer = trace.get_tracer('notifier')
queue = redis.Redis(host='redis')
emails = metrics.get_meter('notifier').create_counter('shop.emails', unit='{email}', description='Order e-mails')


def process(message: dict) -> None:
	# The span continues the trace of the purchase: the e-mail shows in the same waterfall.
	with tracer.start_as_current_span('orders process', context=propagate.extract(message.get('trace', {})),
			kind=SpanKind.CONSUMER, attributes={
				'messaging.system': 'redis', 'messaging.destination.name': 'orders',
				'messaging.operation.type': 'process', 'app.order.id': message['order_id']
			}):
		switches = faults.get()

		with tracer.start_as_current_span('send email'):
			time.sleep(0.05 + switches['notifier_slow_ms'] / 1000)

			if switches['notifier_fail']:
				raise ConnectionError('SMTP server refused the connection (fault panel)')


while True:
	try:
		# Liveness for zabbix-bridge: a worker has no port to probe, so it stamps Redis on every turn of the loop.
		queue.set('notifier:heartbeat', int(time.time()), ex=600)
		item = queue.brpop('orders', timeout=5)
	except redis.RedisError as error:
		log.warning('Queue not reachable: %s', error)
		time.sleep(2)
		continue

	if item is None:
		continue

	order = json.loads(item[1])

	try:
		process(order)
		emails.add(1, {'email.result': 'sent'})
	except Exception as error:
		emails.add(1, {'email.result': 'failed'})
		log.error('E-mail of order %s not sent: %s', order['order_id'], error)
