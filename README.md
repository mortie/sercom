# Sercom - Minimal serial console

Sercom is a serial console which does as little magic as possible -
it leaves things like ANSI escape codes and scrollback to your terminal
emulator.

## Why?

I've had all kinds of trouble with other serial consoles like screen and minicom:

* In Ubuntu, Screen and Minicom would just stop accepting keyboard input
  after the target device reboots. I don't know why.
* Serial consoles are often awkward to use, because they have their own
  scrollback. That means useful features like my terminal's search won't work
  how I'm used to, and using a system via serial just acts different from using
  it via SSH.
* Serial consoles are generally hard to use in scripts. Sercom just reads
  from stdin (and whichever other files you tell it to read), and forwards any
  byte it receives to the serial port.

## Usage

Basic usage of Sercom is:

	sercom <serial port> [baud rate]

When connected, anything you type is forwarded to the serial port, and anything
the other end sends is printed to your stdout.

`Ctrl-a` enters command mode instead of sending the relevant byte. In command
mode, hitting `q` exits Sercom and exits command mode, hitting `Ctrl-a` again
sends the relevant byte (0x01) once and exits command mode, and hitting
any other key exits command mode without sending anything.

## Options

* `-h` or `--help`: Print usage information
* `--no-stdio`: Don't use stdin/stdout.
* `--stdio`: Revert a previous `--no-stdio`.
* `--read <file>`: Read everything from `<file>` and send to the serial port.
* `--write <file>`: Write anything received over the serial port to `<file>`.

## Install

To install Sercom, run:

	git clone github.com/mortie/sercom.git
	cd sercom
	sudo cp sercom.py /usr/local/bin/sercom

You may also need to install Python 3 and the pyserial package. On Ubuntu and
Debian-based distros, that means:

	sudo apt update && sudo apt install python3 python3-serial

To uninstall, just remove the `sercom` file:

	sudo rm /usr/local/bin/sercom

## Automation

The most obvious way to automate something with Sercom is to just write to its
stdin, and that works well. However, sometimes, you want to use the serial
device interactively in addition to automating something.

What I do in that situation is:

1. Create a FIFO with `mkfifo some-filename.fifo`.
2. Ensure the FIFO stays open with `cat >some-filename.fifo &`.
   The `cat` process will just stay in the background and ensure that
   the FIFO doesn't close after the first time we write to it.
3. Run Sercom with `sercom --read some-filename.fifo <port>`.

Now, Sercom will forward anything written to `some-filename.fifo` to the
serial port. `echo ls > some-filename.fifo` will write `ls<newline>`
to the serial port.
