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
* `--snippet <path>`: Read snippets from `<path>`.

## Install

To install Sercom, run:

	git clone https://github.com/mortie/sercom.git
	cd sercom
	sudo cp sercom.py /usr/local/bin/sercom

You may also need to install Python 3 and the pyserial package. On Ubuntu and
Debian-based distros, that means:

	sudo apt update && sudo apt install python3 python3-serial

To uninstall, just remove the `sercom` file:

	sudo rm /usr/local/bin/sercom

## Automation

You can automate something with Sercom by writing to its stdin from a script.
However, there are also other ways:

### --read from a FIFO

With the --read option, sercom will read from a path and write the read bytes
to the serial port. Combined with Linux's FIFOs, this is really powerful:

1. Create a FIFO with `mkfifo some-filename.fifo`.
2. Ensure the FIFO stays open with `cat >some-filename.fifo &`.
   The `cat` process will just stay in the background and ensure that
   the FIFO doesn't close after the first time we write to it.
3. Run Sercom with `sercom --read some-filename.fifo <port>`.

Now, Sercom will forward anything written to `some-filename.fifo` to the
serial port. `echo ls > some-filename.fifo` will write `ls<newline>`
to the serial port.

### Snippets

Sercom also has a concept of snippets. A snippet is a program or file which
you can call at any time. Here's how:

1. Make a snippets directory: `mkdir sercom-snippets`
2. Create a test snippet: `echo Hello World > sercom-snippets/test-snippet`
3. Start Sercom: `sercom <port>`
4. Run the snippet: hit `Ctrl-a :` to enter the REPL, then write
   `test-snippet <enter>`. The text "Hello World" is sent to the serial port.

Sercom will default to looking for snippets in `~/.config/sercom/snippets` and
in `./sercom-snippets` (relative to wherever  you started sercom). Additional
snippet directories can be added with the `--snippet` option.

---

A snippet can also be a script whose stdout is read into the serial port.
For example, a snippet to log in to a Linux system could look like this:

```
#!/bin/sh
echo myusername
sleep 0.1
echo mypassword
```

Put that in `sercom-snippets/login`, make sure it's executable with
`chmod +x sercom-snippets/login`, and then running the `login` snippet
(`<Ctrl-a> :login <enter>`) will enter credentials with a slight delay
between the username and the password.

---

A snippet can also accept arguments. For example, if you have multiple
users on a connected Linux system, it might be useful to have a more complicated
login snippet:

```
#!/bin/sh
if [ "$1" = root ]; then
	echo root
	sleep 0.1
	echo cheesecake
elif [ "$1" = user1 ]; then
	echo user1
	sleep 0.1
	echo password
elif [ "$1" = user2 ]; then
	echo user2
	sleep 0.1
	echo secret
else
	echo "Unknown user: $1" >&2
fi
```

Now, `:login root` will enter root's username/password, while `:login user1`
will enter user1's username/password.
