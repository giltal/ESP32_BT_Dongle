#!/usr/bin/env python3
"""
HCI link test for the ESP32 BT Dongle (no Bluetooth host stack needed, only pyserial).

    python tools/hci_test.py COM19
    python tools/hci_test.py /dev/ttyUSB0 --baud 921600 --try-bauds 1000000,2000000

Steps: reset the board (RTS -> EN), the dongle's vendor identify, HCI Reset, read version /
address / features / buffer sizes, a Classic inquiry, an LE scan, the long-name guard, a
half-duplex stress at the boot baud (one command in flight, like an HCI host), then a full-duplex
line-rate measurement at the boot baud and at each --try-bauds rate (vendor baud switch 0xFC7D).
Exit status 0 = every check passed; the full-duplex table is information only.
"""
import argparse
import struct
import sys
import time

import serial

H4_CMD, H4_ACL, H4_SCO, H4_EVT = 1, 2, 3, 4
OP_IDENTIFY, OP_SET_BAUD, OP_STATS, OP_ECHO = 0xFC7E, 0xFC7D, 0xFC7C, 0xFC7B
STATS_NAMES = ["rx_bytes", "tx_bytes", "rx_pkts", "tx_pkts", "resync_bytes", "dropped_pkts",
               "rx_overflow", "rx_errors"]

MANUFACTURERS = {96: "Espressif", 10: "Qualcomm/CSR", 15: "Broadcom", 93: "Realtek", 2: "Intel"}


class HciError(Exception):
    pass


class Hci:
    def __init__(self, port, baud):
        self.s = serial.Serial()
        self.s.port = port
        self.s.baudrate = baud
        self.s.timeout = 0.05
        # Dev boards wire DTR/RTS to IO0/EN: keep both released when the port opens
        self.s.dtr = False
        self.s.rts = False
        self.s.open()
        if hasattr(self.s, "set_buffer_size"):     # Windows: the driver's default ~4 KiB lasts 45 ms at 921600
            self.s.set_buffer_size(rx_size=1 << 20, tx_size=1 << 16)
        self.buf = bytearray()
        self.events = []        # events that were not the reply being waited for
        self.junk = 0
        self.junk_log = bytearray()     # the last junk bytes (a boot banner or panic text is readable)

    def close(self):
        self.s.close()

    def hw_reset(self, boot_wait=1.5):
        """Pulse EN through RTS (the auto-reset circuit), keep IO0 high (DTR released)."""
        self.s.dtr = False
        self.s.rts = True
        time.sleep(0.1)
        self.s.rts = False
        time.sleep(boot_wait)               # ROM banner at 115200, then the app's controller start
        self.s.reset_input_buffer()
        self.buf.clear()
        self.events.clear()

    def set_baud(self, baud):
        self.s.baudrate = baud

    # --- packets ---
    def send_cmd(self, op, params=b""):
        self.s.write(bytes([H4_CMD]) + struct.pack("<HB", op, len(params)) + params)

    def _read_packet(self, deadline):
        while True:
            # parse what we have
            while self.buf:
                t = self.buf[0]
                if t == H4_EVT:
                    if len(self.buf) >= 3 and len(self.buf) >= 3 + self.buf[2]:
                        p = bytes(self.buf[:3 + self.buf[2]])
                        del self.buf[:3 + self.buf[2]]
                        return p
                    break
                if t == H4_ACL:
                    if len(self.buf) >= 5:
                        n = 5 + (self.buf[3] | self.buf[4] << 8)
                        if len(self.buf) >= n:
                            p = bytes(self.buf[:n])
                            del self.buf[:n]
                            return p
                    break
                self.junk += 1              # not a packet start (boot banner, line noise)
                self.junk_log.append(t)
                del self.junk_log[:-2048]
                del self.buf[0]
            if time.time() > deadline:
                return None
            # take what is there at once: waiting for a full block lets the driver's buffer overflow
            d = self.s.read(self.s.in_waiting or 1)
            if d:
                self.buf += d

    def wait_event(self, match, timeout=2.0):
        deadline = time.time() + timeout
        for i, e in enumerate(self.events):
            if match(e):
                return self.events.pop(i)
        while True:
            p = self._read_packet(deadline)
            if p is None:
                return None
            if p[0] != H4_EVT:
                continue
            if match(p):
                return p
            self.events.append(p)

    def cmd(self, op, params=b"", timeout=2.0, check=True):
        """Send a command, return the Command Complete return parameters (status first)."""
        self.send_cmd(op, params)

        def m(e):
            return (e[1] == 0x0E and len(e) >= 6 and (e[4] | e[5] << 8) == op) or \
                   (e[1] == 0x0F and len(e) >= 7 and (e[5] | e[6] << 8) == op)
        e = self.wait_event(m, timeout)
        if e is None:
            raise HciError(f"no reply to command 0x{op:04X}")
        if e[1] == 0x0F:                    # Command Status
            if check and e[3] != 0:
                raise HciError(f"command 0x{op:04X}: status 0x{e[3]:02X}")
            return bytes([e[3]])
        ret = e[6:]
        if check and ret and ret[0] != 0:
            raise HciError(f"command 0x{op:04X}: status 0x{ret[0]:02X}")
        return ret


def fmt_addr(b):
    return ":".join(f"{x:02X}" for x in reversed(b))


def eir_name(data):
    i = 0
    while i < len(data):
        n = data[i]
        if n == 0 or i + 1 + n > len(data):
            break
        t = data[i + 1]
        if t in (0x08, 0x09):
            return data[i + 2:i + 1 + n].decode("utf-8", "replace")
        i += 1 + n
    return ""


def identify(h):
    r = h.cmd(OP_IDENTIFY)
    if len(r) < 16 or r[1:5] != b"LXBT":
        raise HciError(f"not an LXBT dongle (identify reply {r.hex()})")
    baud = struct.unpack_from("<I", r, 9)[0]
    return dict(proto=r[5], fw=f"{r[6]}.{r[7]}.{r[8]}", baud=baud, flags=r[13], max_acl=r[14], max_le=r[15])


def stats(h):
    r = h.cmd(OP_STATS)
    return dict(zip(STATS_NAMES, struct.unpack_from("<8I", r, 1)))


def info(h):
    h.cmd(0x0C03, timeout=3)                                            # HCI Reset
    r = h.cmd(0x1001)                                                   # Read Local Version
    hci_ver, hci_rev, lmp_ver, manu, lmp_sub = struct.unpack_from("<BHBHH", r, 1)
    print(f"  version     HCI {hci_ver} rev 0x{hci_rev:04X}, LMP {lmp_ver} sub 0x{lmp_sub:04X}, "
          f"manufacturer {manu} ({MANUFACTURERS.get(manu, '?')})")
    print(f"  BD_ADDR     {fmt_addr(h.cmd(0x1009)[1:7])}")
    f = h.cmd(0x1003)[1:9]
    print(f"  features    {f.hex(' ')}  (LE {'yes' if f[4] & 0x40 else 'NO'}, "
          f"BR/EDR {'no' if f[4] & 0x20 else 'yes'}, EDR 2M {'yes' if f[3] & 0x02 else 'no'})")
    r = h.cmd(0x1004, b"\x01", check=False)                             # extended features page 1
    print(f"  ext page 1  " + (r[3:11].hex(" ") if r[0] == 0 else f"status 0x{r[0]:02X}"))
    acl_len, sco_len, acl_n, sco_n = struct.unpack_from("<HBHH", h.cmd(0x1005), 1)
    print(f"  ACL buffers {acl_n} x {acl_len} bytes, SCO {sco_n} x {sco_len}")
    le_len, le_n = struct.unpack_from("<HB", h.cmd(0x2002), 1)
    print(f"  LE buffers  {le_n} x {le_len} bytes" + ("  (shared with ACL)" if le_n == 0 else ""))
    lef = h.cmd(0x2003)[1:9]
    print(f"  LE features {lef.hex(' ')}")
    ok = bool(f[4] & 0x40) and not (f[4] & 0x20) and acl_n > 0 and le_n > 0
    if not ok:
        print("  FAIL: expected BR/EDR + LE with buffers of its own")
    return ok


def inquiry(h, secs):
    h.cmd(0x0C45, b"\x02")                                              # Write Inquiry Mode: extended
    n = max(1, min(0x30, round(secs / 1.28)))
    h.cmd(0x0401, bytes([0x33, 0x8B, 0x9E, n, 0]))                      # Inquiry, GIAC
    found = {}
    done = h.wait_event(lambda e: e[1] in (0x01, 0x02, 0x22, 0x2F), n * 1.28 + 3)
    while done is not None and done[1] != 0x01:
        e = done
        if e[1] == 0x2F:                                                # Extended Inquiry Result
            a = fmt_addr(e[4:10])
            rssi = e[17] - 256 if e[17] > 127 else e[17]
            found[a] = (rssi, eir_name(e[18:]), e[12:15][::-1].hex())
        elif e[1] in (0x02, 0x22):
            for i in range(e[3]):
                a = fmt_addr(e[4 + 6 * i:10 + 6 * i])
                found.setdefault(a, (None, "", ""))
        done = h.wait_event(lambda e: e[1] in (0x01, 0x02, 0x22, 0x2F), n * 1.28 + 3)
    if done is None:
        print("  FAIL: no Inquiry Complete")
        return False
    print(f"  {len(found)} Classic device(s) in {n * 1.28:.1f} s")
    for a, (rssi, name, cod) in sorted(found.items(), key=lambda x: -(x[1][0] or -999)):
        print(f"    {a}  rssi {rssi if rssi is not None else '?':>4}  class {cod or '?':6}  {name}")
    return True


def le_scan(h, secs):
    h.cmd(0x0C01, struct.pack("<Q", 0x20001FFFFFFFFFFF))                # Set Event Mask: + LE Meta
    h.cmd(0x2001, struct.pack("<Q", 0x1F))                              # LE Set Event Mask
    h.cmd(0x200B, bytes([1]) + struct.pack("<HH", 0x60, 0x30) + bytes([0, 0]))  # active scan
    h.cmd(0x200C, b"\x01\x00")                                          # enable, no dup filter
    found = {}
    reports = 0
    end = time.time() + secs
    while time.time() < end:
        e = h.wait_event(lambda e: e[1] == 0x3E and e[3] == 0x02, end - time.time())
        if e is None:
            break
        p = 5
        for _ in range(e[4]):
            reports += 1
            addr = fmt_addr(e[p + 2:p + 8])
            dlen = e[p + 8]
            data = e[p + 9:p + 9 + dlen]
            rssi = e[p + 9 + dlen]
            rssi = rssi - 256 if rssi > 127 else rssi
            name = eir_name(data)
            old = found.get(addr, (rssi, ""))
            found[addr] = (max(rssi, old[0]), name or old[1])
            p += 10 + dlen
    h.cmd(0x200C, b"\x00\x00")
    print(f"  {len(found)} LE device(s), {reports} advertising reports in {secs} s")
    for a, (rssi, name) in sorted(found.items(), key=lambda x: -x[1][0])[:15]:
        print(f"    {a}  rssi {rssi:>4}  {name}")
    if len(found) > 15:
        print(f"    ... and {len(found) - 15} more")
    return reports > 0


def long_name(h):
    """The controller reboots on names of ~78+ bytes; the dongle cuts them to 64."""
    for i in range(3):
        h.cmd(0x0C13, (f"{i} long name " * 30).encode()[:247] + b"\0")
    r = h.cmd(0x0C14)[1:]
    n = r.index(0)
    print(f"  three 247-byte names written, read back {n} bytes: {r[:n].decode()!r}")
    if h.cmd(0x0C14)[0] != 0 or n > 64:
        raise HciError("long name not clamped")
    st = stats(h)
    if st["rx_pkts"] < 5:
        raise HciError(f"dongle restarted (stats {st})")
    return True


def diagnose(h):
    """After a lost reply: which side lost bytes? (host junk = bytes the PC got that made no packet)"""
    seen = [e.hex() for e in h.events[-4:]]
    junk = h.junk
    time.sleep(0.3)
    try:
        st = stats(h)
    except HciError:
        st = "no answer (dongle restarted or link stuck)"
    return f"\n    host junk bytes {junk}, unmatched events {seen}\n    dongle stats {st}"


def stress(h, rounds):
    """Half-duplex load, the way an HCI host with one command in flight works: each round one vendor
    echo of 251 bytes (a 255-byte packet up, 258 down, answered by the dongle's bridge) and one Read
    Local Name (the controller's 255-byte reply, through VHCI). The name is written once: the ESP32
    controller asserts (nvds.c) after a few hundred different names in one boot, which no real host
    does."""
    name = b"LXBT stress".ljust(248, b"\0")
    h.cmd(0x0C13, name)
    junk0 = h.junk
    st0 = stats(h)
    t0 = time.time()
    nbytes = 0
    for i in range(rounds):
        p = bytes((i * 7 + j) & 0xFF for j in range(251))
        e = h.cmd(OP_ECHO, p)
        nbytes += 255 + 258
        if e[1:] != p:
            raise HciError(f"stress round {i}: echo differs" + diagnose(h))
        r = h.cmd(0x0C14)
        nbytes += 4 + 258
        if r[1:249] != name:
            raise HciError(f"stress round {i}: Read Local Name differs" + diagnose(h))
    dt = time.time() - t0
    if h.junk != junk0:
        raise HciError(f"{h.junk - junk0} bytes lost or garbled on the way to the PC" + diagnose(h))
    st = stats(h)
    d = {k: st[k] - st0[k] for k in st}
    print(f"  {rounds} rounds, {nbytes / 1024:.0f} KiB in {dt:.2f} s ({nbytes / dt / 1024:.1f} KiB/s, "
          f"round trips bound)")
    print(f"  dongle counters for the run {d}")
    bad = {k: d[k] for k in ("resync_bytes", "dropped_pkts", "rx_overflow", "rx_errors") if d[k]}
    if bad:
        raise HciError(f"link errors on the dongle side: {bad}")
    return True


def full_duplex(h, n=400):
    """Full-duplex load at line rate: n echoes sent at ~1.5 ms spacing while a reader thread takes the
    replies. Counts intact replies and compares bytes with the dongle's counters, so it tells which
    side lost what. Returns the number of bytes the PC lost."""
    import threading
    st0 = stats(h)
    h.s.reset_input_buffer()
    rx = bytearray()
    stop = []

    def reader():
        while not stop:
            rx.extend(h.s.read(h.s.in_waiting or 1))
    t = threading.Thread(target=reader)
    t.start()
    p = bytes(range(251))
    t0 = time.time()
    for _ in range(n):
        h.send_cmd(OP_ECHO, p)
        time.sleep(0.0015)
    dt = time.time() - t0
    time.sleep(0.5)
    stop.append(1)
    t.join()
    h.buf.clear()
    st = stats(h)
    d = {k: st[k] - st0[k] for k in st}
    exp = bytes([H4_EVT, 0x0E, 255, 1, OP_ECHO & 0xFF, OP_ECHO >> 8, 0]) + p
    intact = rx.count(exp)
    sent_by_dongle = d["tx_bytes"] - 39           # minus the first stats reply (counted after its snapshot)
    lost = sent_by_dongle - len(rx)
    dongle_bad = d["resync_bytes"] + d["dropped_pkts"] + d["rx_overflow"] + d["rx_errors"]
    print(f"  {n} x 255 B up / 258 B down in {dt:.2f} s ({258 * n / dt / 1024:.0f} KiB/s each way): "
          f"{intact}/{n} replies intact, PC lost {lost} of {sent_by_dongle} bytes, "
          f"dongle receive errors {dongle_bad}")
    if dongle_bad or d["rx_pkts"] < n:
        raise HciError(f"dongle side lost data: {d}")
    return lost, n - intact


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=921600, help="the dongle's boot baud (CONFIG_DONGLE_BAUD)")
    ap.add_argument("--try-bauds", default="460800,576000,1000000,1500000,2000000",
                    help="comma list of rates to switch to for the full-duplex measurement (empty = none)")
    ap.add_argument("--no-reset", action="store_true", help="do not pulse EN through RTS")
    ap.add_argument("--inquiry", type=float, default=6.4, help="inquiry seconds (0 = skip)")
    ap.add_argument("--le-scan", type=float, default=5, help="LE scan seconds (0 = skip)")
    ap.add_argument("--rounds", type=int, default=300, help="half-duplex stress rounds")
    ap.add_argument("--duplex", type=int, default=400, help="echoes per full-duplex measurement")
    a = ap.parse_args()

    results = []

    def step(name, fn):
        print(f"\n== {name}")
        try:
            ok = fn()
        except HciError as ex:
            print(f"  FAIL: {ex}")
            ok = False
        results.append((name, ok))
        return ok

    h = Hci(a.port, a.baud)
    if not a.no_reset:
        h.hw_reset()

    def ident():
        d = identify(h)
        print(f"  LXBT dongle, protocol {d['proto']}, firmware {d['fw']}, {d['baud']} baud, "
              f"flags 0x{d['flags']:02X}, max {d['max_acl']} ACL + {d['max_le']} LE links")
        return d["baud"] == a.baud

    if not step(f"identify at {a.baud}", ident):
        print("\nNo dongle answering: wrong port/baud, or the firmware is not flashed.")
        return 1
    step("controller info", lambda: info(h))
    if a.inquiry > 0:
        step("Classic inquiry", lambda: inquiry(h, a.inquiry))
    if a.le_scan > 0:
        step("LE scan", lambda: le_scan(h, a.le_scan))
    step("long local name", lambda: long_name(h))
    step(f"half-duplex stress at {a.baud}", lambda: stress(h, a.rounds))

    # Full duplex at line rate, at the boot rate and each --try-bauds rate: information, not pass/fail
    # (on Windows the CH340 driver loses bytes on the way to the PC at 750000 and up, see README)
    table = []
    for b in [a.baud] + [int(x) for x in a.try_bauds.split(",") if x.strip()]:
        print(f"\n== full duplex at {b}")
        try:
            if b != h.s.baudrate:
                h.cmd(OP_SET_BAUD, struct.pack("<I", b))
                time.sleep(0.02)
                h.set_baud(b)
                h.s.reset_input_buffer()
                h.buf.clear()
                if identify(h)["baud"] != b:
                    raise HciError("dongle did not switch")
            table.append((b, full_duplex(h, a.duplex)))
        except HciError as ex:
            print(f"  FAIL: {ex}")
            table.append((b, None))
            if a.no_reset:
                break
            h.set_baud(a.baud)
            h.hw_reset()
            if not step(f"recover at {a.baud} after reset", ident):
                break

    if not a.no_reset:
        h.set_baud(a.baud)
        h.hw_reset(0.5)
    h.close()

    print("\n== summary")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("  full duplex at line rate (PC-side byte loss):")
    for b, lost in table:
        print(f"    {b:>8}: " + ("link failed" if lost is None else "clean" if lost == (0, 0)
                                  else f"{lost[0]} bytes lost, {lost[1]} replies damaged"))
    return 0 if all(ok for name, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
