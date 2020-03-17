#!/usr/bin/env python3

import serial
import sys
import argparse
import os
import select

parser = argparse.ArgumentParser()
parser.add_argument("device",
    help="Path to the serial port")
parser.add_argument("baud", type=int, default=9600, nargs="?",
    help="Baud rate (default: 9600)")
parser.add_argument("--read", action="append", default=[], metavar="PATH",
    help="Read the content of PATH and send it to the serial port")
parser.add_argument("--write", action="append", default=[], metavar="PATH",
    help="Write anything received from the serial port to PATH")
parser.add_argument("--no-stdio", dest="stdio", action="store_false",
    help="Don't use stdin/stdout")
parser.add_argument("--stdio", action="store_true", default=True,
    help="Revert --no--stdio")
args = parser.parse_args()

cleanups = []

def main():
    inputs = []
    inmap = {}
    outputs = []

    for path in args.read:
        print(f"< {path}", file=sys.stderr)
        inputs.append(open(path, "rb"))
    for path in args.write:
        print(f"> {path}", file=sys.stderr)
        outputs.append(open(path, "wb"))

    ser = serial.Serial(args.device, baudrate=args.baud)
    print(f"Opened {ser.port}, {ser.baudrate} baud.", file=sys.stderr)

    if args.stdio:
        inputs.append(sys.stdin.buffer)
        outputs.append(sys.stdout.buffer)
        if sys.stdin.buffer.isatty():
            print("Hit 'Ctrl-A q' to exit.", file=sys.stderr)
            os.system("stty raw -echo")
            cleanups.append(lambda: os.system("stty sane"))

    poll = select.poll()
    poll.register(ser.fileno(), select.POLLIN | select.POLLPRI)
    for inp in inputs:
        poll.register(inp.fileno(), select.POLLIN | select.POLLPRI)
        inmap[inp.fileno()] = inp

    cmd_mode = False
    def handle_tty_char(ch):
        nonlocal cmd_mode
        if not cmd_mode and ch == 0x01:
            cmd_mode = True
        elif cmd_mode:
            cmd_mode = False
            if ch == ord('q'):
                raise KeyboardInterrupt
            elif ch == 0x01:
                ser.write(ch)
                ser.flush()
        else:
            ser.write(bytes([ch]))
            ser.flush()

    def handle(fd, mask):
        # On bytes from serial, write to outputs
        if fd == ser.fileno():
            chrs = ser.read()
            for out in outputs:
                out.write(chrs)
                out.flush()

        # On message from an input, write to serial
        else:
            f = inmap[fd]
            chrs = os.read(fd, 1024)
            if len(chrs) == 0:
                print(f">> Closed: {f.name}\r", file=sys.stderr)
                inputs.remove(f)
                poll.unregister(f.fileno())
                f.close()
                if len(inputs) == 0:
                    raise KeyboardInterrupt

                return

            if args.stdio and f == sys.stdin.buffer and f.isatty():
                for ch in chrs:
                    handle_tty_char(ch)
            else:
                ser.write(chrs)
                ser.flush()

    while True:
        evts = poll.poll()
        for fd, mask in evts:
            handle(fd, mask)

try:
    main()
except KeyboardInterrupt:
    pass
finally:
    for f in cleanups:
        f()
