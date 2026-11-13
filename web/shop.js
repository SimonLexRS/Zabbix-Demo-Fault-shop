// What the store pages share: the cart id, the API calls and the header.
// Telemetry first: this import ends when the browser SDK is registered, so that the first request is already traced.
import './otel.js';

export const cartId = localStorage.getItem('cart.id') ?? Math.random().toString(36).slice(2, 14);
localStorage.setItem('cart.id', cartId);

export const money = (value) => value.toLocaleString('en-US', { style: 'currency', currency: 'USD' });

// Administrator key of the fault panel (CONTROL_TOKEN), asked once and remembered in this browser.
const KEY = 'controlToken';
let askKey = null;

export function controlKey() {
	try {
		return localStorage.getItem(KEY) ?? '';
	} catch {
		return '';
	}
}

export function setControlKey(key) {
	try {
		key ? localStorage.setItem(KEY, key) : localStorage.removeItem(KEY);
	} catch {
		// Private window or blocked storage: the key lasts only for this call.
	}
}

// The page that changes switches registers how to ask for the key: (message) => Promise<string | null>.
export function onControlKeyNeeded(ask) {
	askKey = ask;
}

async function request(path, options) {
	const headers = { ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }) };
	const key = controlKey();

	if (key) {
		headers['x-control-token'] = key;
	}

	return fetch('/api' + path, { ...options, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
}

export async function api(path, options = {}) {
	let response = await request(path, options);

	// Wrong or missing administrator key: ask for it (up to 3 tries) and repeat the call.
	for (let attempt = 0; response.status === 401 && askKey && attempt < 3; attempt++) {
		setControlKey('');
		const key = await askKey(attempt > 0);

		if (!key) {
			throw new Error('Administrator key required');
		}

		setControlKey(key);
		response = await request(path, options);
	}

	const body = await response.json().catch(() => ({ error: response.statusText }));

	if (!response.ok) {
		throw new Error(`HTTP ${response.status}: ${body.error ?? response.statusText}`);
	}

	return body;
}

export function notice(message, kind = '') {
	const element = document.getElementById('notice');

	element.className = 'notice ' + kind;
	element.textContent = message;
}

export async function addToCart(productId) {
	// The "JavaScript error" switch of the fault panel: the click handler breaks before it calls the API.
	if ((await api('/faults')).js_error) {
		const recommendations = undefined;

		recommendations.refresh(productId);
	}

	const cart = await api('/cart/' + cartId, { method: 'POST', body: { product_id: productId, quantity: 1 } });

	showCartCount(cart.items);
	notice('Added to the cart.', 'ok');
}

export function showCartCount(items) {
	document.getElementById('cart-count').textContent = Object.values(items).reduce((sum, quantity) => sum + quantity, 0);
}

document.querySelector('header').innerHTML = `<nav>
	<a class="brand" href="/">Fault Shop</a>
	<a href="/">Products</a>
	<a href="/cart.html">Cart (<span id="cart-count">0</span>)</a>
	<a href="/faults.html">Fault panel</a>
</nav>`;

api('/cart/' + cartId).then((cart) => showCartCount(cart.items)).catch(() => {});
