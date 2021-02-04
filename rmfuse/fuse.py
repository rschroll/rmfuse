# Copyright 2020-2021 Robert Schroll
# This file is part of rmcl and is distributed under the MIT license.

import argparse
import enum
import errno
import io
import logging
import os
import stat

import bidict
import pyfuse3
import trio

from rmcl import Document, Folder, Item, invalidate_cache
from rmcl.const import ROOT_ID, FileType
from rmcl.exceptions import ApiError, VirtualItemError
from rmcl.utils import now

log = logging.getLogger(__name__)

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
            raise pyfuse3.FUSEError(errno.EINVAL)  # Invalid argument
        return len(buf)


class RmApiFS(pyfuse3.Operations):

    def __init__(self, mode):
        super().__init__()
        self._next_inode = pyfuse3.ROOT_INODE
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

    async def lookup(self, p_inode, name, ctx=None):
        if name == b'.':
            inode = p_inode
        elif name == b'..':
            folder = await self.get_by_id(self.get_id(p_inode))
            if folder.parent is None:
                raise pyfuse3.FUSEError(errno.ENOENT)
            inode = self.get_inode(folder.parent)
        else:
            item = await self.get_by_name(p_inode, name)
            if item:
                inode = self.get_inode(item.id)
            else:
                raise pyfuse3.FUSEError(errno.ENOENT)

        return await self.getattr(inode, ctx)

    async def getattr(self, inode, ctx=None):
        entry = pyfuse3.EntryAttributes()

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

    async def readlink(self, inode, ctx):
        return NotImplemented

    async def opendir(self, inode, ctx):
        return inode

    async def readdir(self, inode, start_id, token):
        item = await self.get_by_id(self.get_id(inode))
        direntries = [item]
        if item.parent is not None:
            direntries.append(await self.get_by_id(item.parent))
        if item.name == '':
            direntries.append(self.mode_file)
        direntries.extend(item.children)
        for i, c in enumerate(direntries[start_id:]):
            pyfuse3.readdir_reply(token, await self.filename(c, item),
                                  await self.getattr(self.get_inode(c.id)),
                                  start_id + i + 1)

    async def open(self, inode, flags, ctx):
        if inode not in self.inode_map:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if (flags & os.O_RDWR or flags & os.O_WRONLY) and self.get_id(inode) != self.mode_file.id:
            raise pyfuse3.FUSEError(errno.EPERM)
        return pyfuse3.FileInfo(fh=inode, direct_io=True)  # direct_io means our size doesn't have to be correct

    async def read(self, fh, start, size):
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
            raise pyfuse3.FUSEError(errno.ENODATA)
        return retval

    async def write(self, fh, offset, buf):
        if self.get_id(fh) == self.mode_file.id:
            return await self.mode_file.write(offset, buf)

        if fh in self.buffers:
            document, data = self.buffers[fh]
            self.buffers[fh] = (document, data[:offset] + buf + data[offset + len(buf):])
            return len(buf)

        raise pyfuse3.FUSEError(errno.EPERM)

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
            raise pyfuse3.FUSEError(errno.EIO)  # Unfortunately, this will be ignored
        try:
            await document.upload(io.BytesIO(data), type_)
        except ApiError as error:
            log.error('API Error:', error)
            raise pyfuse3.FUSEError(errno.EREMOTEIO)  # Unfortunately, this will be ignored
        finally:
            del self.buffers[fh]

    async def rename(self, p_inode_old, name_old, p_inode_new, name_new, flags, ctx):
        item = await self.get_by_name(p_inode_old, name_old)
        if item is None:
            raise pyfuse3.FUSEError(errno.ENOENT)

        basename = name_new.rsplit(b'.', 1)[0]
        if p_inode_old != p_inode_new and await self.get_by_name(p_inode_new, name_new):
            raise pyfuse3.FUSEError(errno.EEXIST)

        parent_new = await self.get_by_id(self.get_id(p_inode_new))
        try:
            item.parent = parent_new.id
            item.name = basename.decode('utf-8')
            await item.update_metadata()
        except ApiError:
            raise pyfuse3.FUSEError(errno.EREMOTEIO)
        except (VirtualItemError, AttributeError):  # AttributeError from .mode file
            raise pyfuse3.FUSEError(errno.EPERM)

    async def unlink(self, p_inode, name, ctx):
        item = await self.get_by_name(p_inode, name)
        if item is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if isinstance(item, Folder):
            raise pyfuse3.FUSEError(errno.EISDIR)
        try:
            await item.delete()
        except ApiError:
            raise pyfuse3.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise pyfuse3.FUSEError(errno.EPERM)

    async def rmdir(self, p_inode, name, ctx):
        item = await self.get_by_name(p_inode, name)
        if item is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        if not isinstance(item, Folder):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if item.children:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        try:
            await item.delete()
        except ApiError:
            raise pyfuse3.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise pyfuse3.FUSEError(errno.EPERM)

    async def create(self, p_inode, name, mode, flags, ctx):
        existing = await self.get_by_name(p_inode, name)
        if existing:
            raise pyfuse3.FUSEError(errno.EEXIST)
        parent = self.get_id(p_inode)
        basename = name.decode('utf-8').rsplit('.', 1)[0]
        document = Document.new(basename, parent)
        inode = self.get_inode(document.id)
        self.buffers[inode] = (document, b'')
        return (pyfuse3.FileInfo(fh=inode, direct_io=True), await self.getattr(inode, ctx))

    async def mkdir(self, p_inode, name, mode, ctx):
        existing = await self.get_by_name(p_inode, name)
        if existing:
            raise pyfuse3.FUSEError(errno.EEXIST)
        parent = self.get_id(p_inode)
        folder = Folder.new(name.decode('utf-8'), parent)
        try:
            await folder.upload()
        except ApiError:
            raise pyfuse3.FUSEError(errno.EREMOTEIO)
        except VirtualItemError:
            raise pyfuse3.FUSEError(errno.EPERM)

        inode = self.get_inode(folder.id)
        return await self.getattr(inode)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('mountpoint', type=str, help="Mount point of filesystem")
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help="Enable verbose output (-vv for even more verbosity)")
    parser.add_argument('-m', '--mode', type=FSMode, choices=list(FSMode),
                        default=FSMode.annot, help="Type of files to mount")
    return parser.parse_args()

def main(options):
    fs = RmApiFS(options.mode)
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add('fsname=rmapi')
    if options.verbose == 1:
        logging.basicConfig(level=logging.DEBUG)
    elif options.verbose > 1:
        logging.basicConfig(level=logging.INFO)
        # Fuse debug is really verbose, so stick that here.
        fuse_options.add('debug')
    pyfuse3.init(fs, options.mountpoint, fuse_options)
    try:
        trio.run(pyfuse3.main)
    finally:
        pyfuse3.close()

if __name__ == '__main__':
    main(parse_args())
