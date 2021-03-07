# RMfuse

RMfuse provides access to your reMarkable Cloud files in the form of a
[FUSE](https://github.com/libfuse/libfuse) filesystem.  These files are
exposed either in their original format, or as PDF files that contain
your annotations.  This lets you manage files in the reMarkable Cloud
using the same tools you use on your local system.

## Installation

RMfuse requires Python 3.7 or later.  It also requires either FUSE3 and
the [pyfuse3 library](https://github.com/libfuse/pyfuse3), or FUSE2 and
the [llfuse library](https://github.com/python-llfuse/python-llfuse).
These can be installed from pip with RMfuse, or installed separately
via your system package manager.  This means that there are three main
ways to get RMfuse installed.

*Confused?  Look below to see examples for several OSes.*

### Install with pyfuse3 through pip

Prior to installing RMfuse, you will need to install FUSE3, along with
its header files, which are probably in a package with a name something
like *libfuse3-devel*.  You will also need a C build system.  Once those
are installed, you can install RMfuse with pip:
```
pip install rmfuse[pyfuse3]
```
Alternatively, you may clone this repository and install with
[Poetry](https://python-poetry.org/):
```
poetry install -E pyfuse3
```

### Install with llfuse through pip

Prior to installing RMfuse, you will need to install FUSE, along with
its header files, which are probably in a package with a name something
like *libfuse-devel*.  You will also need a C build system.  Once those
are installed, you can install RMfuse with pip:
```
pip install rmfuse[llfuse]
```
Alternatively, you may clone this repository and install with
[Poetry](https://python-poetry.org/):
```
poetry install -E llfuse
```

### Install with system FUSE packages

If your system provides either pyfuse3 or llfuse in its package system,
you can install one of them that way.  You can then install RMfuse
without needing to specify either library:
```
pip install rmfuse
```
RMfuse will find whichever library is available at runtime.  (Note that
if you are using a venv, you will need to create it with the
`--system-site-packages` option.  Otherwise, RMfuse will not be able to
see the library you installed.)

### Example installations

Here are some instructions for installations known to succeed.  These are
not the only solutions, so feel free to go another direction.  But if
you're confused by all of the options, you might want to start here.

#### Debian-based systems

Install the FUSE3 libraries and headers with apt:
```
sudo apt install fuse3 libfuse3-3 libfuse3-dev build-essential
```
Then install RMfuse in your chosen environment:
```
pip install rmfuse[pyfuse3]
```
This was tested on Ubuntu 20.04.

#### Fedora-based systems

Install FUSE3 and pyfuse3 with dnf:
```
sudo dnf install fuse3-libs python3-fusepy fuse3-devel python3-devel
```
Be sure to add `~/.local/bin` to your path.  Then install with your
system's pip:
```
pip install rmfuse
```
This was tested on Fedora 33.

#### Arch-based systems

Install RMfuse from this AUR package: [rmfuse](https://aur.archlinux.org/packages/rmfuse/)<sup>AUR</sup>.

[pyfuse3](https://aur.archlinux.org/packages/python-pyfuse3/)<sup>AUR</sup> and [llfuse](https://archlinux.org/packages/community/x86_64/python-llfuse/) are set as optional. Choose the one you prefer and install it.

For example
```
yay -S rmfuse python-pyfuse3
```

#### MacOS

Install [macFuse](https://osxfuse.github.io/) with brew:
```
brew install macfuse pkg-config
```
You will need to allow a kernel extension and reboot.  Then install
RMfuse with the llfuse package:
```
pip install rmfuse[llfuse]
```

There is a [known bug](https://github.com/rschroll/rmfuse/issues/18) with writing large files to RMfuse on a Mac.

#### Windows

As far as I know, no one has tried this.  If you're interested,
[WinFuse](https://github.com/billziss-gh/winfuse) is probably the place
to start.

## Usage

RMfuse installs the script `rmfuse`.  The script takes a single argument,
the path at which the filesystem should be mounted.  This must be an
existing directory.  Any files within that directory will be hidden as
long as RMfuse is mounted.
```
mkdir ~/remarkable
rmfuse ~/remarkable
```
(If you installed with Poetry, you may need to run `poetry run rmfuse`.)

The first time RMfuse is run, it will need a _one-time code_ to get
access to your reMarkable Cloud account.  You will be prompted to get
that code from https://my.remarkable.com/connect/desktop, which may
require logging in to your reMarkable account.  RMfuse uses that code
to obtain tokens which it uses in the future to authenticate itself.

Note that RMfuse will not produce any output by default.  (You can use
`-v` or `-vv` to make it more talkative.)  It does not provide a user
interface to your files; instead you use another terminal or a file
browser to access the mounted directory.

To unmount and halt RMfuse, use the `fusermount` command:
```
fusermount -u ~/remarkable
```

### Modes

RMfuse offers several modes to display your reMarkable Cloud files.  You
can choose the mode with the `-m` option.

`annot`: Displays all files in PDF format, with your annotations added.
This is the default mode.

`orig`: Displays the original file for ebooks and PDF files.  Notebooks
are rendered as PDF files, as in the `annot` mode.

`raw`: Displays all files as ZIP files, reflecting the underlying format
used by the reMarkable Cloud.  This may be useful when working with other
tools that expect files in this form.

`meta`: Displays metadata about the files in JSON format.  Only useful for
debugging.

RMfuse provides a special file named `.mode` in root directory.  When read,
this file gives the current mode.  Writing a valid mode to this file will
switch the mode RMfuse is in.  Additionally, writing `refresh` to this file
will cause RMfuse to refresh its information from the reMarkable Cloud.
(By default, this happens every five minutes.)
```
~/remarkable $ cat .mode
annot
~/remarkable $ ls
book.pdf        document.pdf    notebook.pdf
~/remarkable $ echo orig > .mode
~/remarkable $ ls
book.epub       document.pdf    notebook.pdf
```

### Capabilities

RMfuse allows reading of all files in the reMarkable Cloud.  Since reading
the file requires several HTTP requests, as well as local processing, reads
make take some time.  Running RMfuse in verbose mode (`-v` or `-vv`) will
display information about the actions underway.  Open files are cached, to
improve performance.  More sophisticated caching is planned for the future.

RMfuse does its best to provide accurate metadata for the files.  However,
the reMarkable Cloud provides only modification dates, so that is reported
for creation and access dates as well.  File sizes in `annot` mode are
only estimates until the file is read for the first time.  This metadata
is cached locally to improve responsiveness in the future.

Files can be renamed and moved within the RMfuse filesystem.  These changes
will be propagated to the reMarkable Cloud.  Changes to the file extension
will be ignored.

Deleting files from a RMfuse filesystem moves them into the reMarkable
Cloud's trash area.  These files are accessible in the `.trash` hidden
directory in the root of the file system.  Deleting files within the
`.trash` folder removes them from the reMarkable Cloud.  (_N.B._ It is
not known if this deletes the files from the cloud, or just hides them
from clients.)

EPUB and PDF files may be copied into the filesystem, and new directories
can be created.  These changes are uploaded to the reMarkable Cloud.
Copying other types of files into the RMfuse filesystem will fail silently
(unfortunately).  File extensions are ignored by RMfuse, and thus may
change when files are uploaded.  For instance, if `book.epub` is uploaded
and RMFuse is in `annot` mode, it will show up in the filesystem as
`book.pdf`.

Existing files cannot be edited; they appear in read-only mode. If you
want to edit the contents of a file, you will need to copy it to your
local filesystem, edit it, and then copy it back to the RMfuse filesystem.
This will cause annotations to be lost (in `orig` mode) or flattened into
the document itself (in `annot` mode).

There are several rendering options that affect the appearance of annotated
documents.  These can be set in a config file, which by default lives at
`~/.config/rmfuse/config.ini`.  Run `rmfuse --write-config` to create a
config file with the defaults at that location.  The default mode and
mountpoint can also be set in this file.

## Known Limitations

- The file size for annotated files is just an estimate before the file
is first read.  This can confuse some tools which use the file size to
determine how much to read.  After reading the file once, the file size
will be correctly reported going forward; rerunning these tools a second
time is usually enough to get them working.

- To try to address this, RMfuse throws an error when a program tries to
read past the end of a file.  This can cause "No data available" errors
to be reported.  These are harmless.

- Adding a file other than an EPUB or PDF silently fails.  RMfuse does
throw an error when it has been given an invalid file, but this comes
too late for FUSE to pass the error back to the caller.  RMfuse may be
able to throw an error earlier, based on the first bytes it receives;
this will be investigated in the future.

- RMfuse caches open files in memory.  This is bad for large files (too
much memory used) and small files (we could cache several files).  A more
sophisticated caching system is planned.

## Libraries

RMfuse is powered by [rmcl](https://github.com/rschroll/rmcl), for accessing
the reMarkable Cloud, and by [rmrl](https://github.com/rschroll/rmrl), for
rendering annotated documents.  The early development of RMfuse can be found
in the [rmcl repository](https://github.com/rschroll/rmcl)

## Trademarks

reMarkable(R) is a registered trademark of reMarkable AS. rmrl is not
affiliated with, or endorsed by, reMarkable AS. The use of "reMarkable"
in this work refers to the company’s e-paper tablet product(s).

## Copyright

Copyright 2020-2021 Robert Schroll

RMfuse is released under the MIT license.  See LICENSE.txt for details.

## Disclaimer of Warranty

RMfuse is provided without any warranty.  Users accept the risk of damages,
including the loss of data on their local system, on their reMarkable
device, and in the reMarkable Cloud.

If it breaks, you get to keep both halves.
