# Copyright 2020-2021 Robert Schroll
# This file is part of RMfuse and is distributed under the MIT license.

import argparse
import enum
import errno
import io
import logging
import os
import pkg_resources
import stat

import bidict
import trio

from rmcl import Document, Folder, Item, invalidate_cache
from rmcl.const import ROOT_ID, FileType
from rmcl.exceptions import ApiError, VirtualItemError
from rmcl.utils import now

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
        self.buffers = dict()

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
        return await Item.get_by_id(id_)

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

        if inode in self.buffers:
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
    async def setattr(self, inode, attr, fields, fh, ctx):
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
    async def readlink(self, inode, ctx):
        return NotImplemented

    @async_op
    async def opendir(self, inode, ctx):
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

    @async_op
    async def open(self, inode, flags, ctx):
        if inode not in self.inode_map:
            raise fuse.FUSEError(errno.ENOENT)
        if (flags & os.O_RDWR or flags & os.O_WRONLY) and self.get_id(inode) != self.mode_file.id:
            raise fuse.FUSEError(errno.EPERM)
        return fuse.FileInfo(fh=inode, direct_io=True)  # direct_io means our size doesn't have to be correct

    @async_op
    async def read(self, fh, start, size):
        log.debug(f'Reading from {fh} at {start}, length {size}')
        item = await self.get_by_id(self.get_id(fh))
        if self.mode == FSMode.meta:
            contents = io.BytesIO(f'{item._metadata!r}\n'.encode('utf-8'))
        elif self.mode == FSMode.raw:
            contents = await item.raw()
        elif self.mode == FSMode.annot or (self.mode == FSMode.orig and
                                           await item.type() == FileType.notes):
            contents = await item.annotated()
        elif self.mode == FSMode.orig:
            contents = await item.contents()
        contents.seek(start)
        retval = contents.read(size)
        # Due to inaccurate size estimates, some applications continually
        # try to read past the end of the file, despite consistently getting
        # no data.  Throwing an error alerts them to the problem.
        if not retval:
            raise fuse.FUSEError(errno.ENODATA)
        return retval

    @async_op
    async def write(self, fh, offset, buf):
        if self.get_id(fh) == self.mode_file.id:
            return await self.mode_file.write(offset, buf)

        if fh in self.buffers:
            document, data = self.buffers[fh]
            self.buffers[fh] = (document, data[:offset] + buf + data[offset + len(buf):])
            return len(buf)

        raise fuse.FUSEError(errno.EPERM)

    @async_op
    async def release(self, fh):
        if fh not in self.buffers:
            return

        document, data = self.buffers[fh]
        if data.startswith(b'%PDF'):
            type_ = FileType.pdf
        elif b'mimetypeapplication/epub+zip' in data[:100]:
            type_ = FileType.epub
        else:
            log.error('Error: Not a PDF or EPUB file')
            raise fuse.FUSEError(errno.EIO)  # Unfortunately, this will be ignored
        try:
            await document.upload(io.BytesIO(data), type_)
        except ApiError as error:
            log.error(f'API Error: {error}')
            raise fuse.FUSEError(errno.EREMOTEIO)  # Unfortunately, this will be ignored
        finally:
            del self.buffers[fh]

    @async_op
    async def rename(self, p_inode_old, name_old, p_inode_new, name_new, flags, ctx):
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
    async def unlink(self, p_inode, name, ctx):
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
    async def rmdir(self, p_inode, name, ctx):
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
    async def create(self, p_inode, name, mode, flags, ctx):
        existing = await self.get_by_name(p_inode, name)
        if existing:
            raise fuse.FUSEError(errno.EEXIST)
        parent = self.get_id(p_inode)
        basename = name.decode('utf-8').rsplit('.', 1)[0]
        document = Document.new(basename, parent)
        inode = self.get_inode(document.id)
        self.buffers[inode] = (document, b'')
        return (fuse.FileInfo(fh=inode, direct_io=True), await self._getattr(inode, ctx))

    @async_op
    async def mkdir(self, p_inode, name, mode, ctx):
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
    async def statfs(self, ctx):
        stat = fuse.StatvfsData()
        # Block size, suggests optimal read sizes.  Presumably, we want this
        # large, but I don't know what the limits are.  This is what my root
        # filesystem has.  SSHFS also uses 4096, but sets to 0 on Apple.
        # See https://github.com/libfuse/sshfs/commit/db149d1d874ccf044f3ed8d8f980452506b8fb4b
        stat.f_bsize = stat.f_frsize = 4096

        # Number of blocks.  Some examples set to 0, others to large numbers.
        stat.f_blocks = stat.f_bfree = stat.f_bavail = 2**32 / stat.f_frsize

        # Number of files.  Some set to 0, others to large numbers.
        stat.f_files = stat.f_ffree = stat.f_favail = 10000

        return stat


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('mountpoint', type=str, help="Mount point of filesystem")
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help="Enable verbose output (-vv for even more verbosity)")
    parser.add_argument('-m', '--mode', type=FSMode, choices=list(FSMode),
                        default=FSMode.annot, help="Type of files to mount")
    parser.add_argument('--version', action='version', version=VERSION)
    return parser.parse_args()

def main():
    options = parse_args()
    fs = RmApiFS(options.mode)
    fuse_options = set(fuse.default_options)
    fuse_options.add('fsname=rmapi')
    # From llfuse; causes problems with fuse3
    fuse_options.discard('nonempty')
    if options.verbose == 1:
        logging.basicConfig(level=logging.DEBUG)
    elif options.verbose > 1:
        logging.basicConfig(level=logging.INFO)
        # Fuse debug is really verbose, so stick that here.
        fuse_options.add('debug')
    fuse.init(fs, options.mountpoint, fuse_options)
    try:
        if is_pyfuse3:
            trio.run(fuse.main)
        else:
            fuse.main(workers=1)
    except KeyboardInterrupt:
        log.debug('Exiting due to KeyboardInterrupt')
    finally:
        fuse.close()

if __name__ == '__main__':
    main()
