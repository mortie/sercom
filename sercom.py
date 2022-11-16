#!/usr/bin/env python3

import serial
import sys
import argparse
import os
import select
import cmd
import subprocess
import shlex
import base64
import zlib

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

class Progress:
    def __init__(self, curr, max):
        self.curr = curr
        self.max = max

    def step(self, n):
        self.curr += n
        frac = self.curr / self.max
        print(f"\r>>> [{frac:.2%}] ", file=sys.stderr, end="")

    def done(self):
        print("\r\n", file=sys.stderr, end="")

class B64Encoder:
    def __init__(self):
        self.buf = b""
        self.linechar = 0

    def __call__(self, data):
        data = self.buf + data
        l = len(data)
        while l % 3 != 0: l -= 1
        self.buf = data[l:]
        return self.split64(base64.b64encode(data[:l]))

    def split64(self, data):
        """
        Make sure we output the data in lines which are 64 characters long.
        We do this because some base64 decoders (like openssl) can't deal with
        long lines.
        """
        if self.linechar + len(data) < 64:
            self.linechar += len(data)
            return data

        d = data[:64 - self.linechar]
        first = len(d)
        last = len(data)
        d += b"\n"
        self.linechar = 0
        while last - first >= 64:
            d += data[first:first+64]
            d += b"\n"
            first += 64
        if first != last:
            d += data[first:last]
            self.linechar = last - first
        return d

    def eof(self):
        return self.split64(base64.b64encode(self.buf))

class GZB64Encoder:
    def __init__(self):
        self.compress = zlib.compressobj(wbits=16+15)
        self.b64enc = B64Encoder()

    def __call__(self, data):
        return self.b64enc(self.compress.compress(data))

    def eof(self):
        return self.b64enc(self.compress.flush()) + self.b64enc.eof()

class FileTransfer:
    def __init__(self, dest, encoder, cmd):
        self.encoder = encoder
        self.started = False
        self.dest = dest
        self.cmd = cmd

    def __call__(self, data):
        if self.started:
            return self.encoder(data)
        else:
            self.started = True
            return self.cmd + self.encoder(data)

    def eof(self):
        data = b""
        if not self.started:
            data += cmd
        data += self.encoder.eof()
        data += b"\n\x04"
        return data

    def b64(dest):
        return FileTransfer(
            dest, B64Encoder(),
            b"stty -echo && " +
            b"openssl enc -base64 -d > " + shlex.quote(dest).encode("utf-8") + b" && " +
            b"stty echo\n")

    def gzb64(dest):
        return FileTransfer(
            dest, GZB64Encoder(),
            b"stty -echo && " +
            b"openssl enc -base64 -d -A | gunzip > " + shlex.quote(dest).encode("utf-8") + " && " +
            b"stty echo\n")

cleanups = []
def main():
    inputs = []
    inmap = {}
    outputs = []
    children = []

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
        inmap[inp.fileno()] = (inp, None)

    def do_filetransfer(f, transformer):
        curr = f.fileno()
        f.seek(0, 2)
        max = f.tell()
        f.seek(curr)
        prog = Progress(curr, max)
        interrupted = False
        while True:
            try:
                chrs = f.read(1024)
                if len(chrs) == 0: break
                ser.write(transformer(chrs))
                prog.step(len(chrs))
            except KeyboardInterrupt:
                interrupted = True
                break
        ser.write(transformer.eof())
        prog.done()
        if interrupted:
            raise Exception("Interrupted.")

    def create_cmd():
        class CmdShell(cmd.Cmd):
            prompt = ">>> "
            hidden = ("do_EOF", "do_q")
            ruler = ""
            doc_header = "Commands (type 'help <command>'):"
            identchars = cmd.Cmd.identchars + ".-"

            def get_names(self):
                return [n for n in dir(self.__class__) if n not in self.hidden]

            def do_EOF(self, line):
                print()
                return True

            def do_q(self, line):
                return True

            def do_read(self, line):
                """
                Read a file, and write it to the serial connection.
                Usage: read <path>
                """
                try:
                    path, = shlex.split(line)
                    f = open(os.path.expanduser(path), "rb")
                    self.add_read_file(f)
                    return True
                except Exception as ex:
                    print(ex, file=sys.stderr)

            def do_read_b64(self, line):
                """
                Read a file, and write a base64-encoded version to the serial connection.
                Usage: read_b64 <path>
                """
                try:
                    path, = shlex.split(line)
                    f = open(os.path.expanduser(path), "rb")
                    self.add_read_file(f, B64Encoder())
                    return True
                except Exception as ex:
                    print(ex, file=sys.stderr)

            def do_read_gzb64(self, line):
                """
                Read a file, and write a gzipped, base64-encoded version to the serial connection.
                Usage: read_gzb64 <path>
                """
                try:
                    path, = shlex.split(line)
                    f = open(os.path.expanduser(path), "rb")
                    self.add_read_file(f, GZB64Encoder())
                    return True
                except Exception as ex:
                    print(ex, file=sys.stderr)

            def do_filetransfer(self, line):
                """
                Transfer a file from the host to the target.
                Usage: filetransfer <from> [to]
                """
                try:
                    parts = shlex.split(line)
                    if len(parts) == 1:
                        src = parts[0]
                        dest = os.path.basename(src)
                    else:
                        src, dest = parts
                    f = open(os.path.expanduser(src), "rb")
                    do_filetransfer(f, FileTransfer.b64(dest))
                    return True
                except Exception as ex:
                    print(ex, file=sys.stderr)

            def do_filetransfer_gz(self, line):
                """
                Transfer a file from the host to the target, gzipping it in the process.
                Usage: filetransfer <from> [to]
                """
                try:
                    parts = shlex.split(line)
                    if len(parts) == 1:
                        src = parts[0]
                        dest = os.path.basename(src)
                    else:
                        src, dest = parts
                    f = open(os.path.expanduser(src), "rb")
                    do_filetransfer(f, FileTransfer.gzb64(dest))
                    return True
                except Exception as ex:
                    print(ex, file=sys.stderr)

            def do_ls(self, line):
                """
                List files.
                Usage: ls [paths...]
                """
                paths = shlex.split(line)
                if len(paths) == 0:
                    paths = ['.']

                for path in paths:
                    if len(paths) > 1:
                        print(path + ":")
                    try:
                        print(os.listdir(os.path.expanduser(path)))
                    except Exception as ex:
                        print(ex, file=sys.stderr)

            def add_read_file(self, f, transformer=None):
                inputs.append(f)
                poll.register(f.fileno(), select.POLLIN | select.POLLPRI)
                inmap[f.fileno()] = (f, transformer)

            def add_write_file(self, f):
                outputs.append(f)

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
                        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                    self.add_read_file(proc.stdout)
                    self.add_write_file(proc.stdin)

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
                try:
                    out.write(chrs)
                    out.flush()
                except BrokenPipeError:
                    outputs.remove(out)
                    if out.name in args.write:
                        print(f">> Closed: {out.name}\r", file=sys.stderr)

        # On message from an input, write to serial
        else:
            f, transformer = inmap[fd]
            chrs = os.read(fd, 1024)

            # Empty read means the file closed
            if len(chrs) == 0:
                if f.name in args.read:
                    print(f"<< Closed: {f.name}\r", file=sys.stderr)
                inputs.remove(f)
                poll.unregister(f.fileno())
                f.close()
                if transformer and hasattr(transformer, "eof"):
                    ser.write(transformer.eof())
                if len(inputs) == 0:
                    raise KeyboardInterrupt
                return True

            # stdin has to be handled differently, because of cmd_mode and such
            if args.stdio and f == sys.stdin.buffer and f.isatty():
                for ch in chrs:
                    handle_tty_char(ch)
            else:
                if transformer:
                    ser.write(transformer(chrs))
                else:
                    ser.write(chrs)
                ser.flush()

        return False

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
