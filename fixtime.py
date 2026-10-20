"""
fixtime.py - compare the Windows clock with internet time and correct it.

Uses only the Python standard library. Start it with fixtime.bat.

Windows only lets administrator-level programs change the system clock, so the
script first tries without admin rights. If Windows refuses, it reports how far
off the clock is and asks whether to retry with an administrator prompt.
"""

import ctypes
import datetime as dt
import email.utils
import http.client
import os
import socket
import struct
import sys
import time
import traceback

# Internet time sources, tried in order: NTP servers first, then the Date header
# of plain-HTTP (port 80) web servers for networks that block NTP.
NTP_SERVERS = ["time.windows.com", "time.cloudflare.com", "time.google.com", "pool.ntp.org"]
HTTP_HOSTS = ["www.google.com", "www.microsoft.com", "www.cloudflare.com"]

TOLERANCE_SECONDS = 2.0          # smaller differences count as "correct"
ERROR_PRIVILEGE_NOT_HELD = 1314  # Windows error: this account may not set the clock
NTP_UNIX_DELTA = 2208988800      # seconds between 1900-01-01 (NTP) and 1970-01-01 (Unix)


# ---------------------------------------------------------------- internet time

def _ntp_to_unix(seconds, fraction):
    if not seconds & 0x80000000:  # NTP era 1 (after February 2036)
        seconds += 2 ** 32
    return seconds - NTP_UNIX_DELTA + fraction / 2 ** 32


def parse_ntp_reply(reply, sent, received):
    """Return the seconds to ADD to the system clock to get true time."""
    if len(reply) < 48:
        raise ValueError("reply too short")
    fields = struct.unpack("!4B11I", reply[:48])
    leap_version_mode, stratum = fields[0], fields[1]
    if leap_version_mode & 0x07 != 4 or leap_version_mode >> 6 == 3 or stratum == 0:
        raise ValueError("server is not giving usable time")
    if fields[13] == 0:
        raise ValueError("reply has no timestamp")
    server_received = _ntp_to_unix(fields[11], fields[12])
    server_sent = _ntp_to_unix(fields[13], fields[14])
    return ((server_received - sent) + (server_sent - received)) / 2


def ntp_offset(server, timeout=2.5):
    address = socket.gethostbyname(server)  # look the name up before timing anything
    request = b"\x1b" + 47 * b"\x00"        # NTP client request (version 3)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sent = time.time()
        sock.sendto(request, (address, 123))
        reply, _ = sock.recvfrom(512)
        received = time.time()
    return parse_ntp_reply(reply, sent, received)


def parse_http_date(header):
    moment = email.utils.parsedate_to_datetime(header)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def http_offset(host, timeout=4.0):
    connection = http.client.HTTPConnection(host, 80, timeout=timeout)
    try:
        connection.connect()  # DNS and the TCP handshake happen here, not in the timing
        sent = time.time()
        connection.request("HEAD", "/", headers={"User-Agent": "fixtime", "Connection": "close"})
        response = connection.getresponse()
        received = time.time()
        header = response.getheader("Date")
    finally:
        connection.close()
    if not header:
        raise ValueError("no Date header in the reply")
    # The Date header only has whole seconds, so add half a second on average.
    return parse_http_date(header) + 0.5 - (sent + received) / 2


def get_offset():
    """Return (seconds to add to the system clock, name of the source that answered)."""
    problems = []
    for server in NTP_SERVERS:
        try:
            return ntp_offset(server), "NTP server " + server
        except Exception as exc:
            problems.append("  %s: %s" % (server, exc))
    for host in HTTP_HOSTS:
        try:
            return http_offset(host), "web server " + host
        except Exception as exc:
            problems.append("  %s: %s" % (host, exc))
    raise RuntimeError("\n".join(problems))


# --------------------------------------------------------------------- display

def describe(seconds):
    total = int(round(abs(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
    if minutes:
        parts.append("%d minute%s" % (minutes, "" if minutes == 1 else "s"))
    if secs or not parts:
        parts.append("%d second%s" % (secs, "" if secs == 1 else "s"))
    return " ".join(parts)


def clock_text(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%a %d %b %Y, %I:%M:%S %p")


def zone_text():
    now = dt.datetime.now().astimezone()
    minutes = int((now.utcoffset() or dt.timedelta(0)).total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return "%s (UTC%s%02d:%02d)" % (now.tzname(), sign, hours, mins)


# ----------------------------------------------------------- changing the clock

class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", ctypes.c_ushort),
        ("wMonth", ctypes.c_ushort),
        ("wDayOfWeek", ctypes.c_ushort),
        ("wDay", ctypes.c_ushort),
        ("wHour", ctypes.c_ushort),
        ("wMinute", ctypes.c_ushort),
        ("wSecond", ctypes.c_ushort),
        ("wMilliseconds", ctypes.c_ushort),
    ]


def set_clock(epoch_utc):
    """Set the Windows system clock (UTC). Raises OSError if Windows refuses."""
    moment = dt.datetime.fromtimestamp(epoch_utc, tz=dt.timezone.utc)
    value = SYSTEMTIME(
        moment.year, moment.month, (moment.weekday() + 1) % 7, moment.day,
        moment.hour, moment.minute, moment.second, moment.microsecond // 1000,
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.SetSystemTime(ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())


def relaunch_as_admin():
    """Open an administrator copy of this script. True if Windows started it."""
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    params = '"%s" --elevated' % os.path.abspath(__file__)
    result = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return (result or 0) > 32


def ask_yes_no(question):
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------------------ main

def main(elevated):
    if sys.platform != "win32":
        print("This script only works on Windows.")
        return 1

    print("Checking your clock against internet time...")
    print()
    try:
        offset, source = get_offset()
    except RuntimeError as exc:
        print("Could not reach any time server. Check your internet connection")
        print("(some work and school networks block time servers). Details:")
        print(exc)
        return 1

    now = time.time()
    print("Time source:   %s" % source)
    print("Time zone:     %s" % zone_text())
    print("Your clock:    %s" % clock_text(now))
    print("Correct time:  %s" % clock_text(now + offset))
    print()

    if abs(offset) <= TOLERANCE_SECONDS:
        print("Your clock is accurate, so there is nothing to change.")
        print()
        print("If the time on your screen still looks wrong, the cause is the time zone,")
        print("which can be fixed without admin rights: open Settings > Time & language >")
        print("Date & time, turn off 'Set time zone automatically', pick your time zone,")
        print("and make sure 'Adjust for daylight saving time automatically' is on.")
        return 0

    print("Your clock is %s %s." % (describe(offset), "behind" if offset > 0 else "ahead"))

    try:
        set_clock(time.time() + offset)
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_PRIVILEGE_NOT_HELD:
            print("Could not change the clock: %s" % exc)
            return 1
        print()
        if elevated:
            print("Even with administrator permission Windows refused to change the clock.")
            print("This PC's security policy may block it.")
            return 1
        print("Windows refused: only administrator-level programs may change the clock,")
        print("and this window does not have that permission.")
        print()
        if ask_yes_no("Retry with administrator permission? Windows will ask you to confirm. (y/N): "):
            if relaunch_as_admin():
                print("Opened a new window with administrator permission. Check it for the result.")
                return 0
            print("Could not start an administrator window. Nothing was changed.")
            return 1
        print("Nothing was changed.")
        print()
        print("To skip the prompt in future, an administrator can give your account the")
        print("'Change the system time' right (Local Security Policy, Windows Pro or higher).")
        return 1

    print()
    print("Clock updated. It now shows: %s" % clock_text(time.time()))
    return 0


def run():
    elevated = "--elevated" in sys.argv[1:]
    try:
        code = main(elevated)
    except Exception:
        traceback.print_exc()
        code = 1
    if elevated:  # an administrator window closes by itself, so wait here
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(run())
