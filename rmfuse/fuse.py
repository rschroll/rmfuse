# Copyright 2020-2021 Robert Schroll
# This file is part of RMfuse and is distributed under the MIT license.

import argparse
from collections import defaultdict
import enum
import errno
import io
import logging
import os
import pkg_resources
import platform
import socket
import stat
import sys

import bidict
import trio

from rmcl import Document, Folder, Item, invalidate_cache
from rmcl.api import get_client_s
from rmcl.const import ROOT_ID, FileType
from rmcl.exceptions import ApiError, VirtualItemError
from rmcl.utils import now

from .config import get_config, write_default_config
from .fuselib import async_op, fuse, is_pyfuse3

log = logging.getLogger(__name__)
VERSION = pkg_resources.get_distribution('rmfuse').version

class FSMode(enum.Enum):
    meta = 'meta'
    raw = 'raw'
    orig = 'orig'
    annot = 'annot'

    def __str__(self):
        return self.name


class ModeFile():

    def __init__(self, fs):
        self._fs = fs
        self._metadata = FSMode.meta  # For reading from file in metadata mode

    def __repr__(self):
        return f'<{self.__class__.__name__} "{self.name}">'

    @property
    def name(self):
        return '.mode'

    @property
    def id(self):
        return 'MODE_ID'

    @property
    def parent(self):
        return ''

    @property
    def virtual(self):
        return True

    @property
    def mtime(self):
        return now()

    def _raw_bytes(self):
        return f'{self._fs.mode}\n'.encode('utf-8')

    async def raw(self):
        return io.BytesIO(self._raw_bytes())

    async def raw_size(self):
        return len(self._raw_bytes())

    async def contents(self):
        return await self.raw()

    async def size(self):
        return await self.raw_size()

    async def type(self):
        return 'mode'

    async def update_metadata(self):
        raise VirtualItemError('Cannot update .mode file')

    async def delete(self):
        raise VirtualItemError('Cannot delete .mode file')

    async def write(self, offset, buf):
        command = buf.decode('utf-8').strip().lower()
        if command == 'refresh':
            await invalidate_cache()
            return len(buf)

        try:
            self._fs.mode = FSMode[command]
        except KeyError:
            raise fuse.FUSEError(errno.EINVAL)  # Invalid argument
        return len(buf)

    async def upload(self, new_contents, type_):
        raise VirtualItemError('Cannot upload .mode file')

    async def annotated(self, **kw):
        return await self.raw()

    async def annotated_size(self):
        return await self.raw_size()


class RmApiFS(fuse.Operations):

    def __init__(self, mode):
        super().__init__()
        self._next_inode = fuse.ROOT_INODE
        self.inode_map = bidict.bidict()
        self.inode_map[self.next_inode()] = ''
        self.mode = mode
        self.mode_file = ModeFile(self)
        self.inode_map[self.next_inode()] = self.mode_file.id
        self.write_buffers = dict()
        self.uploading = dict()
        self.read_buffers = dict()
        self.fh_count = defaultdict(int)
        self._prev_read_fail_count = 0

    def next_inode(self):
        value = self._next_inode
        self._next_inode += 1
        return value

    def get_id(self, inode):
        return self.inode_map[inode]

    def get_inode(self, id_):
        if id_ not in self.inode_map.inverse:
            self.inode_map[self.next_inode()] = id_
        return self.inode_map.inverse[id_]

    async def get_by_id(self, id_):
        if id_ == self.mode_file.id:
            return self.mode_file
        try:
            return await Item.get_by_id(id_)
        except KeyError:
            # It may be a newly-created file that hasn't been uploaded yet
            for item, _ in self.write_buffers.values():
                if item.id == id_:
                    return item
            # Or it may be uploading right now.  This may lead to tears.
            for item in self.uploading.values():
                if item.id == id_:
                    logging.warning(f'Getting Item {id_} during upload.  '
                                    'This may lead to odd behavior!')
                    return item
            logging.error(f'Attempt to get non-existent Item {id_}')
            raise fuse.FUSEError(errno.ENOENT)

    async def filename(self, item, pitem=None):
        if item == pitem:
            return b'.'
        if pitem and pitem.parent == item.id:
            return b'..'

        base = item.name.encode('utf-8')
        if isinstance(item, (Folder, ModeFile)):
            return base

        if self.mode == FSMode.raw:
            return base + b'.zip'
        if self.mode == FSMode.annot or (self.mode == FSMode.orig and
                                         await item.type() == FileType.notes):
            return base + b'.pdf'
        if self.mode == FSMode.orig:
            return base + b'.' + str(await item.type()).encode('utf-8')
        return base

    async def get_by_name(self, p_inode, name):
        folder = await self.get_by_id(self.get_id(p_inode))
        if folder.id == ROOT_ID and name == self.mode_file.name.encode('utf-8'):
            return self.mode_file
        for c in folder.children:
            if await self.filename(c) == name:
                return c
        return None

    @async_op
    async def lookup(self, p_inode, name, ctx=None):
        if name == b'.':
            inode = p_inode
        elif name == b'..':
            folder = await self.get_by_id(self.get_id(p_inode))
            if folder.parent is None:
                raise fuse.FUSEError(errno.ENOENT)
            inode = self.get_inode(folder.parent)
        else:
            item = await self.get_by_name(p_inode, name)
            if item:
                inode = self.get_inode(item.id)
            else:
                raise fuse.FUSEError(errno.ENOENT)

        return await self._getattr(inode, ctx)

    async def _getattr(self, inode, ctx=None):
        entry = fuse.EntryAttributes()

        if inode in self.write_buffers:
            entry.st_mode = (stat.S_IFREG | 0o644)
            entry.st_size = 0
            stamp = int(now().timestamp() * 1e9)
        else:
            item = await self.get_by_id(self.get_id(inode))
            if isinstance(item, Document):
                entry.st_mode = (stat.S_IFREG | 0o444)  # TODO: Permissions?
                if self.mode == FSMode.raw:
                    entry.st_size = await item.raw_size()
                elif self.mode == FSMode.annot or (self.mode == FSMode.orig and
                                                   await item.type() == FileType.notes):
                    entry.st_size = await item.annotated_size()
                elif self.mode == FSMode.orig:
                    entry.st_size = await item.size()
                else:
                    entry.st_size = 0
            elif isinstance(item, Folder):
                entry.st_mode = (stat.S_IFDIR | 0o755)
                entry.st_size = 0
            elif isinstance(item, ModeFile):
                entry.st_mode = (stat.S_IFREG | 0o644)
                entry.st_size = await item.size()
            stamp = int(item.mtime.timestamp() * 1e9)

        entry.st_atime_ns = stamp
        entry.st_ctime_ns = stamp
        entry.st_mtime_ns = stamp
        entry.st_gid = os.getgid()
        entry.st_uid = os.getuid()
        entry.st_ino = inode

        return entry

    @async_op
    async def getattr(self, inode, ctx=None):
        return await self._getattr(inode, ctx)

    @async_op
    async def setattr(self, inode, attr, fields, fh, ctx=None):
        # llfuse calls this to truncate a file before writing to it.  We'll
        # just accept the call, but not do anything.
        log.debug(f'setattr called on {await self.get_by_id(self.get_id(inode))!r}')
        if fields.update_atime:
            log.debug(f'  Attempting to set atime to {attr.st_atime_ns}')
        if fields.update_mtime:
            log.debug(f'  Attempting to set mtime to {attr.st_mtime_ns}')
        if fields.update_mode:
            log.debug(f'  Attempting to set mode to {attr.st_mode}')
        if fields.update_uid:
            log.debug(f'  Attempting to set uid to {attr.st_uid}')
        if fields.update_gid:
            log.debug(f'  Attempting to set gid to {attr.st_gid}')
        if fields.update_size:
            log.debug(f'  Attempting to set size to {attr.st_size}')
        log.debug('  No changes made')
        return await self._getattr(inode, ctx)

    @async_op
    async def readlink(self, inode, ctx=None):
        return NotImplemented

    @async_op
    async def opendir(self, inode, ctx=None):
        return inode

    async def _readdir_entries(self, inode):
        item = await self.get_by_id(self.get_id(inode))
        direntries = [item]
        if item.parent is not None:
            direntries.append(await self.get_by_id(item.parent))
        if item.name == '':
            direntries.append(self.mode_file)
        direntries.extend(item.children)
        return item, direntries

    if is_pyfuse3:
        async def readdir(self, inode, start_id, token):
            item, direntries = await self._readdir_entries(inode)
            for i, c in enumerate(direntries[start_id:]):
                fuse.readdir_reply(token, await self.filename(c, item),
                                   await self._getattr(self.get_inode(c.id)),
                                   start_id + i + 1)

    else:
        def readdir(self, inode, start_id):
            item, direntries = trio.run(self._readdir_entries, inode)
            for i, c in enumerate(direntries[start_id:]):
                name = trio.run(self.filename, c, item)
                attrs = trio.run(self._getattr, self.get_inode(c.id))
                yield name, attrs, start_id + i + 1

    def _get_file_handle(self, inode):
        self.fh_count[inode] += 1
        return fuse.FileInfo(fh=inode, direct_io=True)  # direct_io means our size doesn't have to be correct

    @async_op
    async def open(self, inode, flags, ctx=None):
        log.debug(f'Opening inode {inode} with flags {flags}')
        if inode not in self.inode_map:
            raise fuse.FUSEError(errno.ENOENT)
        if (flags & os.O_RDWR or flags & os.O_WRONLY) and self.get_id(inode) != self.mode_file.id:
            raise fuse.FUSEError(errno.EPERM)

        # Get the contents once and cache in anticipation of multiple read calls.
        if inode not in self.read_buffers:
            item = await self.get_by_id(self.get_id(inode))
            if self.mode == FSMode.meta:
                contents = io.BytesIO(f'{item._metadata!r}\n'.encode('utf-8'))
            elif self.mode == FSMode.raw:
                contents = await item.raw()
            elif self.mode == FSMode.annot or (self.mode == FSMode.orig and
                                            await item.type() == FileType.notes):
                contents = await item.annotated(**get_config('render'))
            elif self.mode == FSMode.orig:
                contents = await item.contents()
            self.read_buffers[inode] = contents

        return self._get_file_handle(inode)

    @async_op
    async def read(self, fh, start, size):
        log.debug(f'Reading from {fh} at {start}, length {size}')
        if fh not in self.read_buffers:
            log.error(f'Trying to read from {fh}, but no buffer available')
            raise fuse.FUSEError(errno.ENODATA)

        contents = self.read_buffers[fh]
        contents.seek(start)
        retval = contents.read(size)
        # Due to inaccurate size estimates, some applications continually
        # try to read past the end of the file, despite consistently getting
        # no data.  Throwing an error alerts them to the problem.
        if not retval:
            log.debug(f'  No data available; {self._prev_read_fail_count} previous failures')
            self._prev_read_fail_count += 1
            if self._prev_read_fail_count > 1:
                raise fuse.FUSEError(errno.ENODATA)
        else:
            self._prev_read_fail_count = 0
        return retval

    @async_op
    async def create(self, p_inode, name, mode, flags, ctx=None):
        existing = await self.get_by_name(p_inode, name)
        if existing:
            raise fuse.FUSEError(errno.EEXIST)
        parent = self.get_id(p_inode)
        basename, ext = name.decode('utf-8').rsplit('.', 1)
        if ext not in ('pdf', 'epub'):
            log.warning(f'Trying to create file {name}.  '
                        'If this is not a PDF or EPUB file, it will fail.')
        document = Document.new(basename, parent)
        inode = self.get_inode(document.id)
        self.write_buffers[inode] = (document, b'')
        log.debug(f'Created {basename} for {name}, with inode {inode} and ID {document.id}')
        return (self._get_file_handle(inode), await self._getattr(inode, ctx))

    @async_op
    async def write(self, fh, offset, buf):
        log.debug(f'Writing to {fh} at {offset}, length {len(buf)}')
        if self.get_id(fh) == self.mode_file.id:
            return await self.mode_file.write(offset, buf)

        if fh in self.write_buffers:
            document, data = self.write_buffers[fh]
            self.write_buffers[fh] = (document, data[:offset] + buf + data[offset + len(buf):])
            return len(buf)

        raise fuse.FUSEError(errno.EPERM)

    @async_op
    async def release(self, fh):
        if self.fh_count[fh] > 0:
            self.fh_count[fh] -= 1

        if self.fh_count[fh] > 0:
            log.debug(f'Decremented count on inode {fh}')
            return

        log.debug(f'Releasing inode {fh}')
        if fh in self.read_buffers:
            del self.read_buffers[fh]

        if fh not in self.write_buffers:
            return

        document, data = self.write_buffers.pop(fh)
        if data.startswith(b'%PDF'):
            type_ = FileType.pdf
        elif b'mimetypeapplication/epub+zip' in data[:100]:
            type_ = FileType.epub
        else:
            log.error(f'Error: Not a PDF or EPUB file (file was {document.name})')
            raise fuse.FUSEError(errno.EIO)  # Unfortunately, this will be ignored

        self.uploading[fh] = document
        try:
            await document.upload(io.BytesIO(data), type_)
        except ApiError as error:
            log.error(f'API Error: {error}')
            raise fuse.FUSEError(errno.EREMOTEIO)  # Unfortunately, this will be ignored
        finally:
            del self.uploading[fh]

    @async_op
    async def rename(self, p_inode_old, name_old, p_inode_new, name_new, flags, ctx=None):
        log.debug(f'Renaming {name_old} (in {p_inode_old}) to {name_new} (in {p_inode_new})')
        item = await self.get_by_name(p_inode_old, name_old)
        if item is None:
            raise fuse.FUSEError(errno.ENOENT)

        basename = name_new.rsplit(b'.', 1)[0]
        if p_inode_old != p_inode_new and await self.get_by_name(p_inode_new, name_new):
            raise fuse.FUSEError(errno.EEXIST)

        parent_new = await self.get_by_id(self.get_id(p_inode_new))
        try:
            item.parent = parent_new.id
            item.name = basename.decode('utf-8')
            await item.update_metadata()
        except ApiError:
            raise fuse.FUSEError(errno.EREMOTEIO)
        except (VirtualItemError, AttributeError):  # AttributeError from .mode file
            raise fuse.FUSEError(errno.EPERM)

    @async_op
    async def unlink(self, p_inode, name, ctx=None):
        log.debug(f'Unlinking {name}')
        item = await self.get_by_name(p_inode, name)
        if item is None:
            raise fuse.FUSEError(errno.ENOENT)
        if isinstance(item, Folder):
            raise fuse.FUSEError(errno.EISDIR)
        try:
            await item.delete()
        except ApiError:
            raise fuse.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise fuse.FUSEError(errno.EPERM)

    @async_op
    async def rmdir(self, p_inode, name, ctx=None):
        log.debug(f'Removing directory {name}')
        item = await self.get_by_name(p_inode, name)
        if item is None:
            raise fuse.FUSEError(errno.ENOENT)
        if not isinstance(item, Folder):
            raise fuse.FUSEError(errno.ENOTDIR)
        if item.children:
            raise fuse.FUSEError(errno.ENOTEMPTY)
        try:
            await item.delete()
        except ApiError:
            raise fuse.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise fuse.FUSEError(errno.EPERM)

    @async_op
    async def mkdir(self, p_inode, name, mode, ctx=None):
        log.debug(f'Making directory {name}')
        existing = await self.get_by_name(p_inode, name)
        if existing:
            raise fuse.FUSEError(errno.EEXIST)
        parent = self.get_id(p_inode)
        folder = Folder.new(name.decode('utf-8'), parent)
        try:
            await folder.upload()
        except ApiError:
            raise fuse.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise fuse.FUSEError(errno.EPERM)

        inode = self.get_inode(folder.id)
        return await self._getattr(inode)

    @async_op
    async def statfs(self, ctx=None):
        stats = fuse.StatvfsData()
        # Block size, suggests optimal read sizes.  Presumably, we want this
        # large, but I don't know what the limits are.  This is what my root
        # filesystem has.  SSHFS also uses 4096, but sets to 0 on Apple.
        # See https://github.com/libfuse/sshfs/commit/db149d1d874ccf044f3ed8d8f980452506b8fb4b
        stats.f_bsize = stats.f_frsize = 4096

        # Number of blocks.  Some examples set to 0, others to large numbers.
        stats.f_blocks = stats.f_bfree = stats.f_bavail = 2**32 / stats.f_frsize

        # Number of files.  Some set to 0, others to large numbers.
        stats.f_files = stats.f_ffree = stats.f_favail = 10000

        return stats


class WriteConfigAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        write_default_config()
        parser.exit()

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('mountpoint', type=str, help="Mount point of filesystem")
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help="Enable verbose output (-vv for even more verbosity)")
    parser.add_argument('-m', '--mode', type=FSMode, choices=list(FSMode),
                        default=FSMode.annot, help="Type of files to mount")
    parser.add_argument('--write-config', action=WriteConfigAction, nargs=0,
                        help="Write a default configurations file")
    parser.add_argument('--version', action='version', version=VERSION)
    return parser.parse_args()

def isconnected(host='1.1.1.1', port=80, timeout=1):
    # timeout is expressed in seconds
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except:
        return False

def main():
    options = parse_args()
    fs = RmApiFS(options.mode)
    fuse_options = set(fuse.default_options)
    fuse_options.add('fsname=rmapi')
    # On Macs, don't allow metadata files
    if platform.system() == 'Darwin':
        fuse_options.add('noappledouble')
    # From llfuse; causes problems with fuse3
    fuse_options.discard('nonempty')
    if options.verbose == 1:
        logging.basicConfig(level=logging.DEBUG)
    elif options.verbose > 1:
        logging.basicConfig(level=logging.INFO)
        # Fuse debug is really verbose, so stick that here.
        fuse_options.add('debug')

    if not isconnected():
        log.error('rmfuse cannot get online')
        return errno.EHOSTUNREACH
    if not os.path.isdir(options.mountpoint):
        log.error(f'{options.mountpoint} directory does not exist')
        return errno.ENOTDIR
    if os.path.ismount(options.mountpoint):
        log.error(f'{options.mountpoint} is a mount point already')
        return errno.EEXIST

    # Trigger getting the client, to prompt for one-time code, if needed
    get_client_s()
    fuse.init(fs, options.mountpoint, fuse_options)
    log.debug(f'Mounting on {options.mountpoint}')
    try:
        if is_pyfuse3:
            trio.run(fuse.main)
        else:
            fuse.main(workers=1)
    except KeyboardInterrupt:
        log.debug('Exiting due to KeyboardInterrupt')
    except Exception:
        return 1
    finally:
        fuse.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
