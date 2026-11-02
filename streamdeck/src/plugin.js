// ATK Mouse Battery - Stream Deck plugin.
// Reads http://127.0.0.1:47231/status from the ATK Battery tray app and draws
// the level on every visible key. It never talks to the mouse itself.

import streamDeck, { SingletonAction } from "@elgato/streamdeck";
import { renderKey, toDataUri } from "./render.js";

const STATUS_URL = "http://127.0.0.1:47231/status";
const REFRESH_MS = 15_000;

class BatteryLevel extends SingletonAction {
	manifestId = "com.jb.atk-battery.level";
	timer = null;
	lastImage = null;

	async onWillAppear(ev) {
		await this.draw(ev.action, true);
		this.start();
	}

	onWillDisappear() {
		// stop polling once no keys are showing
		setTimeout(() => {
			if ([...this.actions].length === 0) this.stop();
		}, 0);
	}

	start() {
		if (this.timer) return;
		this.timer = setInterval(() => this.refreshAll(), REFRESH_MS);
		this.refreshAll();
	}

	stop() {
		clearInterval(this.timer);
		this.timer = null;
	}

	async fetchStatus() {
		try {
			const res = await fetch(STATUS_URL, { signal: AbortSignal.timeout(3000) });
			if (!res.ok) return null;
			const json = await res.json();
			return json && json.app === "atk-battery" ? json : null;
		} catch {
			return null;
		}
	}

	async refreshAll() {
		const status = await this.fetchStatus();
		const image = toDataUri(renderKey(status));
		const changed = image !== this.lastImage;
		this.lastImage = image;
		if (!changed) return;
		for (const action of this.actions) {
			if (action.isKey()) await action.setImage(image).catch(() => {});
		}
	}

	async draw(action, force) {
		if (!action.isKey()) return;
		if (force || !this.lastImage) {
			this.lastImage = toDataUri(renderKey(await this.fetchStatus()));
		}
		await action.setImage(this.lastImage).catch(() => {});
	}
}

streamDeck.logger.setLevel("info");
streamDeck.actions.registerAction(new BatteryLevel());
streamDeck.connect();
