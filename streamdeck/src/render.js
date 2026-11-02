// Draws the 144x144 key as SVG. Pure function so it can be previewed offline.

const GREEN = "#2e9e5b";
const AMBER = "#d99a1e";
const RED = "#d64541";
const GREY = "#6e6e76";

function colourFor(percent, stale) {
	if (percent == null || stale) return GREY;
	if (percent <= 15) return RED;
	if (percent <= 35) return AMBER;
	return GREEN;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/**
 * @param {object|null} status  /status JSON from the tray app, or null if unreachable
 * @returns {string} SVG markup
 */
export function renderKey(status) {
	let percent = null;
	let charging = false;
	let stale = true;
	let label = "";

	if (!status) {
		label = "Tray app off";
	} else if (status.percent == null) {
		label = status.connected ? "Wake mouse" : "No mouse";
	} else {
		percent = status.percent;
		charging = !!status.charging;
		stale = !status.awake;
		label = charging ? "Charging" : stale ? "Asleep" : "Mouse";
	}

	const colour = colourFor(percent, stale);
	// battery body 96x44 at (20,22), terminal nub on the right
	const bodyX = 20, bodyY = 22, bodyW = 96, bodyH = 44, inset = 6;
	const fillMax = bodyW - inset * 2;
	const fillW = percent == null ? 0 : Math.max(6, Math.round((fillMax * percent) / 100));
	const text = percent == null ? "--" : `${percent}%`;
	const fontSize = text.length >= 4 ? 40 : 46;

	const bolt = charging
		? `<path d="M72 25 L56 47 L67 47 L62 63 L80 40 L69 40 L74 25 Z" fill="#ffd60a" stroke="#141416" stroke-width="2.5" stroke-linejoin="round"/>`
		: "";

	return `<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">
<rect width="144" height="144" fill="#141416"/>
<rect x="${bodyX}" y="${bodyY}" width="${bodyW}" height="${bodyH}" rx="9" fill="none" stroke="#e8e8ec" stroke-width="5"/>
<rect x="${bodyX + bodyW + 3}" y="${bodyY + 14}" width="7" height="16" rx="2" fill="#e8e8ec"/>
<rect x="${bodyX + inset}" y="${bodyY + inset}" width="${fillW}" height="${bodyH - inset * 2}" rx="4" fill="${colour}"/>
${bolt}
<text x="72" y="112" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-weight="700" font-size="${fontSize}" fill="${percent == null ? "#9a9aa2" : "#ffffff"}">${esc(text)}</text>
<text x="72" y="134" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-weight="600" font-size="15" fill="${stale ? "#9a9aa2" : colour}">${esc(label)}</text>
</svg>`;
}

export function toDataUri(svg) {
	return "data:image/svg+xml;charset=utf8," + encodeURIComponent(svg);
}
