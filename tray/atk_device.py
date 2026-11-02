"""
Finding the ATK receiver/mouse and talking to it over hidapi.

The dongle exposes several HID collections. Only the vendor collection that
carries report 0x08 answers commands, and Windows lists more than one
collection with the same usage page, so each candidate is probed with a
power request and the first one that answers with a valid frame is kept.
"""

import logging
import threading
import time

import hid

import atk_protocol as p

log = logging.getLogger("atk.device")

ATK_VID = 0x373B

# Collections to try, best first. 0xFF02:0x02 is where the 0x08 framing was
# captured; the vendor HUB also lists 0xFF04:0x02 and 0xFF05:0x01 (NearLink).
PREFERRED_COLLECTIONS = [(0xFF02, 0x0002), (0xFF04, 0x0002), (0xFF05, 0x0001)]

# Known names from the ATK HUB device table (receiver/cable product ids)
KNOWN_PIDS = {
    0x127F: "ATK A9 Air +",
    0x1288: "ATK A9 Air +",
    0x1293: "ATK A9 Air +",
    0x120A: "ATK A9 Air",
}
# Receivers known to top out at 1000 Hz
ONE_K_RECEIVERS = {0x101A}

REPLY_TIMEOUT = 0.6      # per attempt
REQUEST_ATTEMPTS = 3     # re-send if the mouse was dozing
FLUSH_TIMEOUT_MS = 30
WRITE_SETTLE = 0.25


class NotConnected(Exception):
    pass


class NoAnswer(Exception):
    """The receiver is there but the mouse didn't reply (asleep / off / out of range)."""


def candidates():
    """vendor-collection entries for any ATK device, best first."""
    found = []
    for d in hid.enumerate(ATK_VID, 0):
        up, u = d.get("usage_page", 0), d.get("usage", 0)
        if (up, u) in PREFERRED_COLLECTIONS:
            rank = PREFERRED_COLLECTIONS.index((up, u))
        elif up >= 0xFF00:
            rank = 10          # other vendor page: try last
        elif up == 0:
            rank = 20          # platforms that don't report usage pages
        else:
            continue           # mouse / keyboard / consumer collections never answer
        found.append((rank, d))
    found.sort(key=lambda t: (t[0], t[1].get("product_id", 0)))
    return [d for _, d in found]


def describe(d) -> str:
    return (f"{d.get('vendor_id', 0):04x}:{d.get('product_id', 0):04x} "
            f"page {d.get('usage_page', 0):04x}:{d.get('usage', 0):04x} "
            f"if {d.get('interface_number', -1)} "
            f"'{d.get('product_string') or ''}'")


class AtkMouse:
    """
    Remembers which HID collection answers, but opens a FRESH handle for every
    transaction and closes it afterwards.

    Windows queues input reports per open handle (up to 32). Keeping one handle
    open meant replies that arrived late, mouse event pushes, and copies of
    replies to ATK HUB's own requests piled up in that queue, and later reads
    picked up those old frames - a frozen battery level and a polling read-back
    that showed the previous rate. A new handle starts with an empty queue.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.info = None          # hid.enumerate dict for the collection that answers
        self.name = None

    # ---- connection -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.info is not None

    def close(self):
        with self._lock:
            self.info = None

    def connect(self) -> bool:
        """
        Find the collection that answers a power request. Returns True if found.
        Raises NoAnswer if a receiver exists but nothing answered (mouse asleep).
        """
        with self._lock:
            self.info = None
            cands = candidates()
            if not cands:
                return False
            saw_writable = False
            for d in cands:
                try:
                    body = self._exchange_on(d, p.build_power(), p.is_power_reply)
                except NoAnswer:
                    saw_writable = True
                    log.info("no answer on %s", describe(d))
                    continue
                except NotConnected as e:
                    log.info("skipped %s: %s", describe(d), e)
                    continue
                self.info = d
                self.name = (KNOWN_PIDS.get(d.get("product_id"))
                             or (d.get("product_string") or "ATK mouse").strip())
                log.info("using %s (power reply %s)", describe(d), p.hexs(body))
                return True
            if saw_writable:
                raise NoAnswer("receiver found, mouse not answering")
            return False

    def max_polling_hz(self) -> int:
        if self.info and self.info.get("product_id") in ONE_K_RECEIVERS:
            return 1000
        name = (self.info or {}).get("product_string") or ""
        if "1k" in name.lower():
            return 1000
        return 8000

    # ---- raw exchange -----------------------------------------------------

    @staticmethod
    def _open(d):
        dev = hid.device()
        try:
            dev.open_path(d["path"])
        except Exception as e:
            raise NotConnected(f"open failed: {e}")
        return dev

    @staticmethod
    def _drain(dev):
        # Real timeouts, not 0: a zero-timeout read on Windows can return
        # nothing while frames are still queued.
        for _ in range(40):
            try:
                if not dev.read(64, FLUSH_TIMEOUT_MS):
                    return
            except Exception:
                return

    @staticmethod
    def _write(dev, body):
        log.debug("-> %s", p.hexs(body))
        try:
            n = dev.write(p.to_report(body))
        except Exception as e:
            raise NotConnected(f"write failed: {e}")
        if n is None or n < 0:
            err = ""
            try:
                err = dev.error()
            except Exception:
                pass
            raise NotConnected(f"write refused ({err})")

    @staticmethod
    def _await(dev, matches, timeout):
        deadline = time.monotonic() + timeout
        while True:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                return None
            try:
                raw = dev.read(64, left)
            except Exception as e:
                raise NotConnected(f"read failed: {e}")
            frame = p.normalise(raw)
            if frame is None:
                continue
            log.debug("<- %s", p.hexs(frame))
            if matches(frame):
                return frame

    def _exchange_on(self, d, body, matches, attempts=REQUEST_ATTEMPTS):
        """Open, flush, send (re-sending if needed), wait for a matching reply, close."""
        dev = self._open(d)
        try:
            self._drain(dev)
            for _ in range(attempts):
                self._write(dev, body)
                frame = self._await(dev, matches, REPLY_TIMEOUT)
                if frame is not None:
                    return frame
            raise NoAnswer("timed out")
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _exchange(self, body, matches):
        if self.info is None:
            raise NotConnected("no collection selected")
        try:
            return self._exchange_on(self.info, body, matches)
        except NotConnected:
            self.info = None
            raise

    # ---- commands ---------------------------------------------------------

    def read_battery(self) -> dict:
        with self._lock:
            body = self._exchange(p.build_power(), p.is_power_reply)
            log.info("power reply %s", p.hexs(body))
            return p.decode_power(body)

    def _read_eeprom(self, addr, length) -> bytes:
        body = self._exchange(p.build_read(addr, length),
                              lambda f: p.is_read_reply(f, addr, length))
        return body[p.DATA_OFFSET:p.DATA_OFFSET + length]

    def read_polling(self) -> int:
        with self._lock:
            return p.decode_polling(self._read_eeprom(p.SYSTEM_ADDR, p.SYSTEM_LEN))

    def set_polling(self, hz: int) -> int:
        """Write, then read back on a fresh handle. Returns the rate the mouse kept."""
        if hz not in p.POLL_HZ_TO_CODE:
            raise ValueError(f"{hz} Hz is not a valid rate")
        with self._lock:
            if self.info is None:
                raise NotConnected("no collection selected")
            before = None
            try:
                before = self.read_polling()
            except Exception as e:
                log.info("pre-write polling read failed: %s", e)
            write_body = p.build_write(p.SYSTEM_ADDR, p.polling_pair(hz))
            dev = self._open(self.info)
            try:
                self._drain(dev)
                self._write(dev, write_body)
                ack = self._await(dev, lambda f: p.valid(f) and f[0] == p.CMD_WRITE_EEPROM, 0.4)
                log.info("polling write %s Hz: %s ack %s", hz, p.hexs(write_body),
                         p.hexs(ack) if ack else "(none)")
            finally:
                dev.close()
            time.sleep(WRITE_SETTLE)
            confirmed = None
            for _ in range(4):
                try:
                    confirmed = self.read_polling()
                    break
                except NoAnswer:
                    time.sleep(0.2)
            log.info("polling before=%s requested=%s read back=%s", before, hz, confirmed)
            if confirmed is None:
                raise NoAnswer("mouse did not confirm the new rate")
            return confirmed


# ---- diagnostics ------------------------------------------------------------

def probe_report() -> str:
    """Plain-text dump of every ATK collection and what it answers."""
    lines = [f"hidapi enumerate for VID {ATK_VID:04x}:"]
    allc = hid.enumerate(ATK_VID, 0)
    if not allc:
        lines.append("  (nothing found - is the receiver or cable plugged in?)")
        return "\n".join(lines)
    for d in allc:
        lines.append("  " + describe(d))
    lines.append("")
    lines.append("Probing candidate collections with a power request:")
    for d in candidates():
        lines.append("  " + describe(d))
        dev = hid.device()
        try:
            dev.open_path(d["path"])
        except Exception as e:
            lines.append(f"    open failed: {e}")
            continue
        try:
            req = p.to_report(p.build_power())
            lines.append(f"    -> {p.hexs(req)}")
            n = dev.write(req)
            lines.append(f"    write returned {n}")
            if n is not None and n >= 0:
                end = time.monotonic() + 0.8
                got = False
                while time.monotonic() < end:
                    raw = dev.read(64, 200)
                    if raw:
                        got = True
                        frame = p.normalise(raw)
                        tag = ""
                        if frame and p.is_power_reply(frame):
                            tag = f"  => {p.decode_power(frame)}"
                        lines.append(f"    <- {p.hexs(raw)}{tag}")
                if not got:
                    lines.append("    (no reply)")
                # also read the system row (polling rate)
                req = p.to_report(p.build_read(p.SYSTEM_ADDR, p.SYSTEM_LEN))
                lines.append(f"    -> {p.hexs(req)}")
                if dev.write(req) >= 0:
                    end = time.monotonic() + 0.8
                    while time.monotonic() < end:
                        raw = dev.read(64, 200)
                        if raw:
                            frame = p.normalise(raw)
                            tag = ""
                            if frame and p.is_read_reply(frame, 0, p.SYSTEM_LEN):
                                try:
                                    tag = f"  => {p.decode_polling(frame[p.DATA_OFFSET:])} Hz"
                                except p.ProtocolError as e:
                                    tag = f"  => {e}"
                            lines.append(f"    <- {p.hexs(raw)}{tag}")
        except Exception as e:
            lines.append(f"    error: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
    return "\n".join(lines)
