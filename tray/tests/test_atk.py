"""
Offline tests: protocol frames against a real ATK A9 Plus capture
(OpenMouse captures/atk-a9-plus-nk) and the device logic against a fake
receiver. Run:  python -m pytest tests  (or python tests/test_atk.py)
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import atk_protocol as p


def h(s):
    return bytes.fromhex(s.replace(" ", ""))


# --- real capture (report id included) ---------------------------------------
CAPTURE = [
    ("08 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 49",
     "08 04 00 00 00 02 64 00 10 9a 00 00 00 00 00 00 39"),
    ("08 08 00 00 00 06 00 00 00 00 00 00 00 00 00 00 3f",
     "08 08 00 00 00 06 01 54 01 54 00 55 00 00 00 00 40"),
    ("08 10 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3d",
     "08 10 00 00 00 02 02 53 00 00 00 00 00 00 00 00 e6"),
]


def test_requests_match_capture():
    assert p.to_report(p.build_power()) == h(CAPTURE[0][0])
    assert p.to_report(p.build_read(0x0000, 6)) == h(CAPTURE[1][0])
    assert p.to_report(p.build(p.CMD_CIDMID)) == h(CAPTURE[2][0])


def test_replies_validate_and_decode():
    for _, reply in CAPTURE:
        body = p.normalise(h(reply))
        assert p.valid(body), reply
    bat = p.decode_power(p.normalise(h(CAPTURE[0][1])))
    assert bat == {"percent": 100, "charging": False, "millivolts": 4250}
    sysrow = p.normalise(h(CAPTURE[1][1]))
    assert p.is_read_reply(sysrow, 0, 6)
    assert p.decode_polling(sysrow[p.DATA_OFFSET:]) == 1000


def test_charging_vector_from_openmouse_tests():
    body = h("04 00 00 00 02 64 01 10 8e 00 00 00 00 00 00 44")
    assert p.valid(body)
    assert p.decode_power(body) == {"percent": 100, "charging": True, "millivolts": 4238}


def test_bad_checksum_rejected():
    body = bytearray(h(CAPTURE[0][1])[1:])
    body[6] ^= 1
    assert not p.is_power_reply(bytes(body))


def test_polling_pairs():
    for hz in p.ALL_RATES:
        code, chk = p.polling_pair(hz)
        assert (code + chk) & 0xFF == 0x55
        assert p.decode_polling(bytes([code, chk])) == hz
    w = p.build_write(0, p.polling_pair(8000))
    assert w[:7] == bytes([0x07, 0, 0, 0, 2, 0x40, 0x15])
    assert p.valid(w)


# --- fake receiver ----------------------------------------------------------

class FakeMouse:
    def __init__(self):
        self.eeprom = bytearray(256)
        self.eeprom[0:6] = bytes([0x01, 0x54, 0x01, 0x54, 0x00, 0x55])
        self.asleep = False
        self.max_hz = 8000
        self.percent, self.charging, self.mv = 73, 0, 3950
        self.doze_drops = 0

    def handle(self, body):
        if self.asleep or not p.valid(body):
            return None
        cmd = body[0]
        out = bytearray(16)
        out[0] = cmd
        if cmd == p.CMD_POWER:
            out[4] = 2
            out[5:9] = bytes([self.percent, self.charging, self.mv >> 8, self.mv & 0xFF])
        elif cmd == p.CMD_READ_EEPROM:
            addr, n = (body[2] << 8) | body[3], body[4]
            out[2:5] = body[2:5]
            out[5:5 + n] = self.eeprom[addr:addr + n]
        elif cmd == p.CMD_WRITE_EEPROM:
            addr, n = (body[2] << 8) | body[3], body[4]
            data = body[5:5 + n]
            if addr == 0 and p.POLL_CODE_TO_HZ.get(data[0], 0) > self.max_hz:
                data = self.eeprom[0:2]  # receiver silently keeps old rate
            self.eeprom[addr:addr + n] = data
            out[2:5] = body[2:5]
        else:
            return None
        out[15] = p.checksum(out[:15])
        return bytes([p.REPORT_ID]) + bytes(out)


MOUSE = FakeMouse()
OPEN_HANDLES = []


def broadcast(frame):
    """Windows delivers each input report to every open handle's own queue."""
    for d in OPEN_HANDLES:
        d.queue.append(list(frame))


class FakeDevice:
    def __init__(self):
        self.path = None
        self.queue = []

    def open_path(self, path):
        self.path = path
        OPEN_HANDLES.append(self)

    def write(self, data):
        if self.path != b"vendor08":
            return -1  # wrong collection: Windows refuses the report id
        if MOUSE.doze_drops > 0:     # mouse dozing: ignores this request
            MOUSE.doze_drops -= 1
            return 17
        r = MOUSE.handle(bytes(data[1:17]))
        if r:
            broadcast(r)
        return 17

    def read(self, n, timeout=0):
        if timeout == 0:
            return []    # Windows overlapped read: zero timeout often sees nothing
        return self.queue.pop(0) if self.queue else []

    def error(self):
        return "refused"

    def close(self):
        if self in OPEN_HANDLES:
            OPEN_HANDLES.remove(self)


def fake_hid():
    m = types.ModuleType("hid")
    m.device = FakeDevice
    m.enumerate = lambda vid, pid: [
        {"path": b"mouse", "vendor_id": 0x373B, "product_id": 0x127F, "usage_page": 1, "usage": 2},
        {"path": b"vendor13", "vendor_id": 0x373B, "product_id": 0x127F, "usage_page": 0xFF02, "usage": 2},
        {"path": b"vendor08", "vendor_id": 0x373B, "product_id": 0x127F, "usage_page": 0xFF02, "usage": 2,
         "product_string": "2.4G Receiver"},
        {"path": b"ff04", "vendor_id": 0x373B, "product_id": 0x127F, "usage_page": 0xFF04, "usage": 2},
    ]
    return m


def load_device():
    sys.modules["hid"] = fake_hid()
    import importlib
    import atk_device
    importlib.reload(atk_device)
    atk_device.REPLY_TIMEOUT = 0.05
    atk_device.WRITE_SETTLE = 0
    atk_device.FLUSH_TIMEOUT_MS = 1
    return atk_device


def test_connect_skips_refusing_collection_and_reads():
    D = load_device()
    MOUSE.asleep = False
    m = D.AtkMouse()
    assert m.connect()
    assert m.info["path"] == b"vendor08"
    assert m.name == "ATK A9 Air +"
    assert m.read_battery() == {"percent": 73, "charging": False, "millivolts": 3950}
    assert m.read_polling() == 1000


def test_set_polling_and_receiver_limit():
    D = load_device()
    MOUSE.asleep = False
    m = D.AtkMouse()
    m.connect()
    assert m.set_polling(4000) == 4000
    assert m.read_polling() == 4000
    MOUSE.max_hz = 1000
    assert m.set_polling(8000) == 4000   # kept old rate -> app reports it
    MOUSE.max_hz = 8000
    assert m.set_polling(1000) == 1000


def test_asleep_raises_no_answer():
    D = load_device()
    MOUSE.asleep = True
    m = D.AtkMouse()
    try:
        m.connect()
        raise AssertionError("expected NoAnswer")
    except D.NoAnswer:
        pass
    MOUSE.asleep = False
    assert m.connect()


def test_battery_tracks_changes_despite_other_traffic():
    """The bug JB hit: level frozen at its first value."""
    D = load_device()
    MOUSE.asleep = False
    MOUSE.percent = 65
    m = D.AtkMouse()
    assert m.connect()
    assert m.read_battery()["percent"] == 65
    # ATK HUB (another open handle) polls a lot meanwhile
    hub = FakeDevice(); hub.open_path(b"vendor08")
    for _ in range(20):
        hub.write(bytes([8]) + p.build_power())
    for level in (64, 60, 55):
        MOUSE.percent = level
        hub.write(bytes([8]) + p.build_power())
        assert m.read_battery()["percent"] == level
    hub.close()
    assert not OPEN_HANDLES, "a handle was left open"


def test_dozing_mouse_answers_on_resend():
    D = load_device()
    MOUSE.asleep = False
    m = D.AtkMouse()
    m.connect()
    MOUSE.doze_drops = 2
    MOUSE.percent = 50
    assert m.read_battery()["percent"] == 50


def test_polling_readback_not_stale():
    D = load_device()
    MOUSE.asleep = False
    m = D.AtkMouse()
    m.connect()
    m.set_polling(1000)
    hub = FakeDevice(); hub.open_path(b"vendor08")
    for _ in range(10):
        hub.write(bytes([8]) + p.build_read(0, 6))   # stale 1000 Hz rows in flight
    assert m.set_polling(2000) == 2000
    assert m.read_polling() == 2000
    hub.close()
    m.set_polling(1000)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
