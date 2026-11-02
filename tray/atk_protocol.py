"""
ATK / VXE vendor HID protocol (A9 family), as documented by the OpenMouse
project (drivers/atk) and Halo Battery (providers/pulsar.py), and matching a
real ATK A9 Plus capture.

Every exchange is one output report and one input report, report id 0x08,
with a 16-byte body:

    [cmd, 0x00, addr_hi, addr_lo, length, data x10, checksum]

checksum = (0x55 - (0x08 + sum(body[0:15]))) & 0xFF

Commands used here:
    0x04  power: data = [percent, charging, mV_hi, mV_lo]
    0x08  read EEPROM (addr, length)
    0x07  write EEPROM (addr, length, data)

EEPROM 0x0000 holds the polling rate as a value/check pair: [code, 0x55 - code].
"""

REPORT_ID = 0x08
BODY_LEN = 16
DATA_OFFSET = 5
CHECK_TARGET = 0x55

CMD_POWER = 0x04
CMD_WRITE_EEPROM = 0x07
CMD_READ_EEPROM = 0x08
CMD_CIDMID = 0x10

SYSTEM_ADDR = 0x0000
SYSTEM_LEN = 6

# polling-rate code <-> Hz
POLL_CODE_TO_HZ = {
    0x08: 125,
    0x04: 250,
    0x02: 500,
    0x01: 1000,
    0x10: 2000,
    0x20: 4000,
    0x40: 8000,
}
POLL_HZ_TO_CODE = {hz: code for code, hz in POLL_CODE_TO_HZ.items()}
ALL_RATES = sorted(POLL_HZ_TO_CODE)


class ProtocolError(Exception):
    pass


def checksum(body15) -> int:
    return (CHECK_TARGET - ((REPORT_ID + sum(b & 0xFF for b in body15)) & 0xFF)) & 0xFF


def build(cmd: int, after_cmd=()) -> bytes:
    """16-byte body (no report id)."""
    body = bytearray(BODY_LEN)
    body[0] = cmd & 0xFF
    for i, b in enumerate(list(after_cmd)[:14]):
        body[i + 1] = b & 0xFF
    body[15] = checksum(body[:15])
    return bytes(body)


def build_power() -> bytes:
    return build(CMD_POWER)


def build_read(addr: int, length: int) -> bytes:
    return build(CMD_READ_EEPROM, [0, (addr >> 8) & 0xFF, addr & 0xFF, length])


def build_write(addr: int, data) -> bytes:
    data = list(data)
    return build(CMD_WRITE_EEPROM, [0, (addr >> 8) & 0xFF, addr & 0xFF, len(data), *data])


def to_report(body: bytes) -> bytes:
    """What hidapi.write() wants: report id first."""
    return bytes([REPORT_ID]) + body


def normalise(raw) -> bytes | None:
    """
    Turn whatever hidapi.read() returned into a 16-byte body, or None.
    Windows includes the report id as byte 0; some stacks don't.
    """
    if not raw:
        return None
    raw = bytes(raw)
    if len(raw) >= BODY_LEN + 1 and raw[0] == REPORT_ID:
        raw = raw[1:BODY_LEN + 1]
    elif len(raw) >= BODY_LEN:
        raw = raw[:BODY_LEN]
    else:
        return None
    return raw


def valid(body: bytes) -> bool:
    return len(body) == BODY_LEN and body[15] == checksum(body[:15])


def is_power_reply(body: bytes) -> bool:
    return valid(body) and body[0] == CMD_POWER and body[1] == 0


def is_read_reply(body: bytes, addr: int, length: int) -> bool:
    # some firmware answers a read with the write command id
    return (valid(body)
            and body[0] in (CMD_READ_EEPROM, CMD_WRITE_EEPROM)
            and body[1] == 0
            and body[2] == (addr >> 8) & 0xFF
            and body[3] == addr & 0xFF
            and body[4] >= length)


def decode_power(body: bytes) -> dict:
    """
    -> {'percent', 'charging', 'millivolts'}
    The declared length is sometimes 2 even though voltage bytes follow,
    so voltage is taken whenever it looks like a Li-ion cell.
    """
    n = body[4]
    d = body[DATA_OFFSET:]
    percent = min(d[0], 100) if n >= 1 else None
    charging = (d[1] != 0) if n >= 2 else None
    mv = (d[2] << 8) | d[3]
    millivolts = mv if 2800 <= mv <= 4600 else None
    if percent is None:
        raise ProtocolError("power reply carried no level")
    return {"percent": percent, "charging": charging, "millivolts": millivolts}


def decode_polling(system: bytes) -> int:
    code, check = system[0], system[1]
    if (code + check) & 0xFF != CHECK_TARGET:
        raise ProtocolError(f"polling value failed its check ({code:02x} {check:02x})")
    if code not in POLL_CODE_TO_HZ:
        raise ProtocolError(f"unknown polling code 0x{code:02x}")
    return POLL_CODE_TO_HZ[code]


def polling_pair(hz: int) -> list[int]:
    code = POLL_HZ_TO_CODE[hz]
    return [code, (CHECK_TARGET - code) & 0xFF]


def hexs(b) -> str:
    return " ".join(f"{x:02x}" for x in bytes(b))
