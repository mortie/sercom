#!/usr/bin/env python3

import serial
import sys
import argparse
import os
import select
import cmd
import time
import subprocess

def default_config_dir():
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if not xdg_config_home:
        xdg_config_home = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg_config_home, "sercom")

def default_shell():
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    else:
        return "/bin/sh"

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
parser.add_argument("--snippets", action="append",
    default=[os.path.join(default_config_dir(), "snippets"), "sercom-snippets"], metavar="PATH",
    help="Read snippets from PATH")
args = parser.parse_args()

def stty_raw():
    os.system("stty raw -echo")
def stty_sane():
    os.system("stty sane")

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
            print("Hit 'Ctrl-A q' to exit, 'Ctrl-A :' to enter a REPL.", file=sys.stderr)
            stty_raw()
            cleanups.append(stty_sane)

    poll = select.poll()

    poll.register(ser.fileno(), select.POLLIN | select.POLLPRI)
    for inp in inputs:
        poll.register(inp.fileno(), select.POLLIN | select.POLLPRI)
        inmap[inp.fileno()] = inp

    def create_cmd():
        class CmdShell(cmd.Cmd):
            prompt = ">>> "
            hidden = ("do_EOF", "do_q")
            ruler = ""
            doc_header = "Commands (type 'help <command>'):"

            def get_names(self):
                return [n for n in dir(self.__class__) if n not in self.hidden]

            def do_EOF(self, line):
                print()
                return True

            def do_q(self, line):
                return True

            def add_read_file(self, f):
                inputs.append(f)
                poll.register(f.fileno(), select.POLLIN | select.POLLPRI)
                inmap[f.fileno()] = f

            def run_snippet(self, path, line):
                with open(path, "rb") as f:
                    shebang = f.read(2)

                if shebang != b"#!":
                    self.add_read_file(open(path, "rb"))
                else:
                    f.close()
                    env = os.environ.copy()
                    env["SNIPPET"] = path
                    proc = subprocess.Popen([default_shell(), "-c", f"\"$SNIPPET\" {line}"],
                        env=env, stdout=subprocess.PIPE)
                    self.add_read_file(proc.stdout)

        def add_snippet(path, fname):
            name, ext = os.path.splitext(fname)
            def do_snippet(self, line):
                self.run_snippet(path, line)
                return True
            do_snippet.__name__ = "do_" + name
            do_snippet.__doc__ = "Run the snippet from '" + path + "'."
            setattr(CmdShell, do_snippet.__name__, do_snippet)

        for snippetdir in args.snippets:
            if not os.path.isdir(snippetdir):
                continue
            for snippet in os.listdir(snippetdir):
                add_snippet(os.path.join(snippetdir, snippet), snippet)

        return CmdShell()

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
                ser.write(bytes([ch]))
                ser.flush()
            elif ch == ord(':'):
                sys.stdout.buffer.write(b"\033[2K\r")
                stty_sane()
                try:
                    create_cmd().cmdloop()
                except KeyboardInterrupt:
                    print()
                stty_raw()
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

            # Empty read means the file closed
            if len(chrs) == 0:
                if f.name in args.read:
                    print(f">> Closed: {f.name}\r", file=sys.stderr)
                inputs.remove(f)
                poll.unregister(f.fileno())
                f.close()
                if len(inputs) == 0:
                    raise KeyboardInterrupt
                return

            # stdin has to be handled differently, because of cmd_mode and such
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
