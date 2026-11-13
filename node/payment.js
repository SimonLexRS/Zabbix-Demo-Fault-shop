// payment: charges a card through the bank, an outside service.
'use strict';

const express = require('express');
const { metrics, trace, SpanStatusCode } = require('@opentelemetry/api');
const faults = require('./faults');

const BANK = 'http://bank:8000/authorize';
const BANK_TIMEOUT_MS = 4000;
const CARDS = { 4: 'visa', 5: 'mastercard', 3: 'amex' };

class CardDeclinedError extends Error {
	constructor(message) {
		super(message);
		this.name = 'CardDeclinedError';
	}
}

class BankError extends Error {
	constructor(name, message) {
		super(message);
		this.name = name;
	}
}

// Business KPI: charges by card and result (APM -> Business KPIs).
const payments = metrics.getMeter('payment').createCounter('shop.payments', { unit: '{payment}', description: 'Card charges' });
const RESULTS = { CardDeclinedError: 'declined', BankTimeoutError: 'bank_timeout', BankUnreachableError: 'bank_unreachable' };

const app = express();

app.use(express.json());
app.use(faults.down('payment'));

// For zabbix-bridge: after the "down" switch, so it answers 503 while the panel has payment off.
app.get('/health', (req, res) => res.json({ ok: true, service: 'payment' }));

app.post('/charge', async (req, res) => {
	const cardType = CARDS[String(req.body.card || '')[0]] || 'unknown';

	trace.getActiveSpan()?.setAttributes({ 'app.payment.amount': Number(req.body.amount), 'app.payment.card_type': cardType });

	try {
		await charge(req.body.amount);
		payments.add(1, { 'payment.card_type': cardType, 'payment.result': 'approved' });
	}
	catch (error) {
		payments.add(1, { 'payment.card_type': cardType, 'payment.result': RESULTS[error.name] || 'error' });
		throw error;
	}

	res.json({ charged: req.body.amount });
});

async function charge(amount) {
	if (Math.random() * 100 < faults.get().payment_fail_pct) {
		throw new CardDeclinedError('Card declined by the issuer (fault panel)');
	}

	const response = await fetch(BANK, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ amount }),
		signal: AbortSignal.timeout(BANK_TIMEOUT_MS)
	}).catch((error) => {
		// fetch reports both as a DOMException or a bare TypeError: name what happened.
		throw error.name === 'TimeoutError'
			? new BankError('BankTimeoutError', `The bank did not answer in ${BANK_TIMEOUT_MS / 1000} s`)
			: new BankError('BankUnreachableError', `The bank is not reachable: ${error.cause?.message ?? error.message}`);
	});

	if (!response.ok) {
		throw new Error(`The bank answered HTTP ${response.status}`);
	}
}

app.use(faults.failed(trace, SpanStatusCode));
app.listen(8000, () => console.log('payment listening on 8000'));
