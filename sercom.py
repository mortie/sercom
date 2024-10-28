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
import time

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

def human_size(size):
    if size < 1024:
        return f"{size:.02f}"
    elif size < 1024 * 1024:
        return f"{size / 1024:.02f}k"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.02f}M"
    else:
        return f"{size / (1024 * 1024 * 1024):.02f}G"

def human_time(secs):
    secs = int(secs)
    if secs < 0:
        return "?"
    elif secs < 60:
        return f"{secs}s"
    elif secs < 60 * 60:
        return f"{int(secs / 60)}m {secs % 60}s"
    else:
        hh = int(secs / (60 * 60))
        secs = secs % (60 * 60)
        return f"{hh}h {int(secs / 60)}m {secs % 60}s"

class Progress:
    def __init__(self, curr, max):
        self.curr = curr
        self.max = max
        self.rate = 0
        self.acc = 0
        self.time = time.time()
        self.start_time = time.time()
        self.rate_calc_time = 1

    def step(self, n):
        self.curr += n
        self.acc += n
        frac = self.curr / self.max
        now = time.time()
        if now - self.time > self.rate_calc_time:
            self.rate = self.acc / self.rate_calc_time
            self.time += self.rate_calc_time
            self.acc = 0
            if self.rate_calc_time < 10:
                self.rate_calc_time += 1

        if self.rate == 0:
            eta_secs = -1
        else:
            eta_secs = (self.max - self.curr) / self.rate

        print(
            "\033[2K\r>>> " +
            f"[{frac:.2%}] " +
            f"[{human_size(self.curr)} / {human_size(self.max)}] " +
            f"[{human_size(self.rate)}/s] " +
            f"[ETA: {human_time(eta_secs)}] ",
            file=sys.stderr, end="")

    def done(self):
        print("\r\n", file=sys.stderr, end="")

class B64Encoder:
    def __init__(self):
        self.buf = b""

    def __call__(self, data):
        data = self.buf + data
        end = len(data)
        while end % 48 != 0:
            end -= 1
        output = b""
        start = 0
        while end - start >= 48:
            output += base64.b64encode(data[start:start+48])
            output += b"\n"
            start += 48
        self.buf = data[start:]

        # Because of this:
        # https://github.com/rockchip-linux/kernel/blob/9ed2be4b9c001ca8006cb4c72928c09927c44f89/drivers/soc/rockchip/rk_fiq_debugger.c#L170
        return output.replace(b"fiq", b"fi\nq")

    def eof(self):
        if len(self.buf) > 0:
            return base64.b64encode(self.buf) + b"\n"
        else:
            return b""

class GZB64Encoder:
    def __init__(self):
        self.compress = zlib.compressobj(wbits=16+15)
        self.b64enc = B64Encoder()

    def __call__(self, data):
        return self.b64enc(self.compress.compress(data))

    def eof(self):
        d = self.b64enc(self.compress.flush())
        d += self.b64enc.eof()
        return d

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

    def transfer_file_to_serial(f, dest, transformer, recvcmd):
        curr = f.tell()
        f.seek(0, 2)
        max = f.tell()
        f.seek(curr)
        prog = Progress(curr, max)
        interrupted = False

        def read_until_str(s):
            start = time.time()
            d = b""
            while time.time() < start + 2:
                d += os.read(ser.fileno(), 100)
                if s in d:
                    return
            sys.stderr.buffer.write(b"Expected the other end to write: '" + s + b"', but it didn't!\n")
            has_line = False
            parts = d.split(b'\n')
            for part in parts:
                if part.strip() == b"": continue
                if not has_line:
                    sys.stderr.buffer.write(b"Instead, it wrote:\n")
                    has_line = True
                sys.stderr.buffer.write(b"  " + part + b"\n")
            raise Exception("File transfer failed.")

        # Make sure we're actually looking at a terminal.
        # Doing it this way ensures that A) the target has some vaguely posix-shell-like shell,
        # and B) since we haven't disabled echo yet, we'll read back what we wrote.
        ser.write(b"echo '===SERCOM''::''FILETRANSFER==='\n")
        read_until_str(b"===SERCOM::FILETRANSFER===")

        full_recvcmd = recvcmd + b" > " + shlex.quote(dest).encode("utf-8")

        # Verify that we can touch the destination file
        ser.write(b"touch " + shlex.quote(dest).encode("utf-8") + b" && echo '===SERCOM''::''COMMAND_OK==='\n")
        read_until_str(b"===SERCOM::COMMAND_OK===")

        # Make the receiving end not write back the stuff we send, since that's a lot of data
        ser.write(b"stty -echo && echo '===SERCOM''::''STTY_OK==='\n")
        read_until_str(b"===SERCOM::STTY_OK===")

        # Prepare the other end for receiving data
        ser.write(full_recvcmd + b'\n')
        time.sleep(0.1)

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
        ser.write(b"\n\x04")
        ser.write(b"stty echo && echo '===SERCOM''::''STTY_OK==='\n")
        read_until_str(b"===SERCOM::STTY_OK===")

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
                    transfer_file_to_serial(f, dest, B64Encoder(), b"openssl enc -base64 -d")
                except Exception as ex:
                    print(ex, file=sys.stderr)
                return True

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
                    transfer_file_to_serial(f, dest, GZB64Encoder(), b"openssl enc -base64 -d | gunzip")
                except Exception as ex:
                    print(ex, file=sys.stderr)
                return True

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
                print("\nExiting...\r", file=sys.stderr)
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
