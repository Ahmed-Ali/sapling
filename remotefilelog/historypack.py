import lz4, mmap, os, struct, tempfile
from collections import defaultdict, deque
from mercurial import mdiff, util
from mercurial.node import nullid, bin, hex
from mercurial.i18n import _
import shallowutil

# (filename hash, offset, size)
INDEXFORMAT = '!20sQQ'
INDEXENTRYLENGTH = 36
NODELENGTH = 20

# (node, p1, p2, linknode)
PACKFORMAT = "!20s20s20s20s"
PACKENTRYLENGTH = 80

# The fanout prefix is the number of bytes that can be addressed by the fanout
# table. Example: a fanout prefix of 1 means we use the first byte of a hash to
# look in the fanout table (which will be 2^8 entries long).
FANOUTPREFIX = 2
# The struct pack format for fanout table location (i.e. the format that
# converts the node prefix into an integer location in the fanout table).
FANOUTSTRUCT = '!H'
# The number of fanout table entries
FANOUTCOUNT = 2**(FANOUTPREFIX * 8)
# The total bytes used by the fanout table
FANOUTENTRYSTRUCT = '!I'
FANOUTENTRYSIZE = 4
FANOUTSIZE = FANOUTCOUNT * FANOUTENTRYSIZE

INDEXSUFFIX = '.histidx'
PACKSUFFIX = '.histpack'

class AncestorIndicies(object):
    NODE = 0
    P1NODE = 1
    P2NODE = 2
    LINKNODE = 3

class historypackstore(object):
    def __init__(self, path):
        self.packs = []
        suffixlen = len(INDEXSUFFIX)
        for root, dirs, files in os.walk(path):
            for filename in files:
                if (filename[-suffixlen:] == INDEXSUFFIX
                    and ('%s%s' % (filename[:-suffixlen], PACKSUFFIX)) in files):
                    packpath = os.path.join(root, filename)
                    self.packs.append(historypack(packpath[:-suffixlen]))

    def getmissing(self, keys):
        missing = keys
        for pack in self.packs:
            missing = pack.getmissing(missing)

        return missing

    def getparents(self, name, node):
        for pack in self.packs:
            try:
                return pack.getparents(name, node)
            except KeyError as ex:
                pass

        raise KeyError((name, node))

    def getancestors(self, name, node):
        for pack in self.packs:
            try:
                return pack.getancestors(name, node)
            except KeyError as ex:
                pass

        raise KeyError((name, node))

    def getlinknode(self, name, node):
        for pack in self.packs:
            try:
                return pack.getlinknode(name, node)
            except KeyError as ex:
                pass

        raise KeyError((name, node))

    def add(self, name, node, data):
        raise Exception("cannot add to historypackstore (%s:%s)"
                        % (name, hex(node)))

class historypack(object):
    def __init__(self, path):
        self.path = path
        self.packpath = path + PACKSUFFIX
        self.indexpath = path + INDEXSUFFIX
        self.indexfp = open(self.indexpath, 'r+b')
        self.datafp = open(self.packpath, 'r+b')

        self.indexsize = os.stat(self.indexpath).st_size
        self.datasize = os.stat(self.packpath).st_size

        # memory-map the file, size 0 means whole file
        self._index = mmap.mmap(self.indexfp.fileno(), 0)
        self._data = mmap.mmap(self.datafp.fileno(), 0)

        rawfanout = self._index[:FANOUTSIZE]
        self._fanouttable = []
        for i in range(0, FANOUTCOUNT):
            loc = i * FANOUTENTRYSIZE
            fanoutentry = struct.unpack(FANOUTENTRYSTRUCT,
                    rawfanout[loc:loc + FANOUTENTRYSIZE])[0]
            self._fanouttable.append(fanoutentry)

    def getmissing(self, keys):
        missing = []
        for name, node in keys:
            value = self._find(node)
            if not value:
                missing.append((name, node))

        return missing

    def getparents(self, name, node):
        section = self._findsection(name)
        node, p1, p2, linknode = self._findnode(section, node)
        return p1, p2

    def getancestors(self, name, node):
        """Returns as many ancestors as we're aware of.

        return value: {
           node: (p1, p2, linknode),
           ...
        }
        """
        filename, offset, size = self._findsection(name)
        ancestors = set((node,))
        results = {}
        for o in range(offset, offset + size, PACKENTRYLENGTH):
            entry = struct.unpack(PACKFORMAT,
                                  self._data[o:o + PACKENTRYLENGTH])
            if entry[AncestorIndicies.NODE] in ancestors:
                ancestors.add(entry[AncestorIndicies.P1NODE])
                ancestors.add(entry[AncestorIndicies.P2NODE])
                result = (entry[AncestorIndicies.P1NODE],
                          entry[AncestorIndicies.P2NODE],
                          entry[AncestorIndicies.LINKNODE],
                          # Add a fake None for the copyfrom entry for now
                          # TODO: remove copyfrom from getancestor api
                          None)
                results[entry[AncestorIndicies.NODE]] = result

        if not results:
            raise KeyError((name, node))
        return results

    def getlinknode(self, name, node):
        section = self._findsection(name)
        node, p1, p2, linknode = self._findnode(section, node)
        return linknode

    def add(self, name, node, data):
        raise RuntimeError("cannot add to historypack" % (name, hex(node)))

    def _findnode(self, section, node):
        name, offset, size = section
        data = self._data
        for i in range(offset, offset + size, PACKENTRYLENGTH):
            entry = struct.unpack(PACKFORMAT,
                                  data[offset:offset + PACKENTRYLENGTH])
            if entry[0] == node:
                return entry

        raise KeyError("unable to find history for %s:%s" % (name, hex(node)))

    def _findsection(self, name):
        namehash = util.sha1(name).digest()
        fanoutkey = struct.unpack(FANOUTSTRUCT, namehash[:FANOUTPREFIX])[0]
        fanout = self._fanouttable

        start = fanout[fanoutkey] + FANOUTSIZE
        if fanoutkey < FANOUTCOUNT - 1:
            end = self._fanouttable[fanoutkey + 1] + FANOUTSIZE
        else:
            end = self.indexsize

        # Bisect between start and end to find node
        index = self._index
        startnode = self._index[start:start + NODELENGTH]
        endnode = self._index[end:end + NODELENGTH]
        if startnode == namehash:
            entry = self._index[start:start + INDEXENTRYLENGTH]
        elif endnode == namehash:
            entry = self._index[end:end + INDEXENTRYLENGTH]
        else:
            iteration = 0
            while start < end - INDEXENTRYLENGTH:
                iteration += 1
                mid = start  + (end - start) / 2
                mid = mid - ((mid - FANOUTSIZE) % INDEXENTRYLENGTH)
                midnode = self._index[mid:mid + NODELENGTH]
                if midnode == namehash:
                    entry = self._index[mid:mid + INDEXENTRYLENGTH]
                    break
                if namehash > midnode:
                    start = mid
                    startnode = midnode
                elif namehash < midnode:
                    end = mid
                    endnode = midnode
            else:
                raise KeyError(name)

        filenamehash, offset, size = struct.unpack(INDEXFORMAT, entry)
        filenamelength = struct.unpack('!H', self._data[offset:offset + 2])[0]
        actualname = self._data[offset + 2:offset + 2 + filenamelength]
        if name != actualname:
            raise KeyError("found file name %s when looking for %s" %
                           (actualname, name))
        return (name, offset + 2 + filenamelength, size - filenamelength - 2)

class mutablehistorypack(object):
    """A class for constructing and serializing a histpack file and index.

    A history pack is a pair of files that contain the revision history for
    various file revisions in Mercurial. It contains only revision history (like
    parent pointers and linknodes), not any revision content information.

    It consists of two files, with the following format:

    .histpack
        The pack itself is a series of file revisions with some basic header
        information on each.

        datapack = <version: 1 byte>
                   [<filesection>,...]
        filesection = <filename len: 2 byte unsigned int>
                      <filename>
                      <revision count: 4 byte unsigned int>
                      [<revision>,...]
        revision = <node: 20 byte>
                   <p1node: 20 byte>
                   <p2node: 20 byte>
                   <linknode: 20 byte>

        The revisions within each filesection are stored in topological order
        (newest first).

    .histidx
        The index file provides a mapping from filename to the file section in
        the histpack. It consists of two parts, the fanout and the index.

        The index is a list of index entries, sorted by filename hash (one per
        file section in the pack). Each entry has:

        - node (The 20 byte hash of the filename)
        - pack entry offset (The location of this file section in the histpack)
        - pack content size (The on-disk length of this file section's pack data)

        The fanout is a quick lookup table to reduce the number of steps for
        bisecting the index. It is a series of 4 byte pointers to positions
        within the index. It has 2^16 entries, which corresponds to hash
        prefixes [00, 01, 02,..., FD, FE, FF]. Example: the pointer in slot 4F
        points to the index position of the first revision whose node starts
        with 4F. This saves log(2^16) bisect steps.

        dataidx = <fanouttable>
                  <index>
        fanouttable = [<index offset: 4 byte unsigned int>,...] (2^16 entries)
        index = [<index entry>,...]
        indexentry = <node: 20 byte>
                     <pack file section offset: 8 byte unsigned int>
                     <pack file section size: 8 byte unsigned int>
    """
    def __init__(self, path):
        self.path = path
        self.entries = []
        self.packfp, self.historypackpath = tempfile.mkstemp(suffix=PACKSUFFIX + '-tmp', dir=path)
        self.idxfp, self.historyidxpath = tempfile.mkstemp(suffix=INDEXSUFFIX + '-tmp', dir=path)
        self.packfp = os.fdopen(self.packfp, 'w+')
        self.idxfp = os.fdopen(self.idxfp, 'w+')
        self.sha = util.sha1()

        # Write header
        # TODO: make it extensible
        version = struct.pack('!B', 0) # unsigned 1 byte int
        self.writeraw(version)

        self.pastfiles = {}
        self.currentfile = None
        self.currentfilestart = 0

    def add(self, filename, node, p1, p2, linknode):
        if filename != self.currentfile:
            if filename in self.pastfiles:
                raise Exception("cannot add file node after another file's "
                                "nodes have been added")
            if self.currentfile:
                self.pastfiles[self.currentfile] = (
                    self.currentfilestart,
                    self.packfp.tell() - self.currentfilestart
                )
            self.currentfile = filename
            self.currentfilestart = self.packfp.tell()
            # TODO: prefix the filename section with the number of entries
            self.writeraw("%s%s" % (
                struct.pack('!H', len(filename)),
                filename,
            ))

        rawdata = struct.pack('!20s20s20s20s', node, p1, p2, linknode)
        self.writeraw(rawdata)

    def writeraw(self, data):
        self.packfp.write(data)
        self.sha.update(data)

    def close(self):
        if self.currentfile:
            self.pastfiles[self.currentfile] = (
                self.currentfilestart,
                self.packfp.tell() - self.currentfilestart
            )

        sha = self.sha.hexdigest()
        self.packfp.close()
        self.writeindex()

        os.rename(self.historypackpath, os.path.join(self.path, sha +
                                                     PACKSUFFIX))
        os.rename(self.historyidxpath, os.path.join(self.path, sha +
                                                    INDEXSUFFIX))

    def writeindex(self):
        files = ((util.sha1(node).digest(), offset, size)
                for node, (offset, size) in self.pastfiles.iteritems())
        files = sorted(files)
        rawindex = ""

        fanouttable = [-1] * FANOUTCOUNT

        count = 0
        for namehash, offset, size in files:
            location = count * INDEXENTRYLENGTH
            count += 1

            fanoutkey = struct.unpack(FANOUTSTRUCT, namehash[:FANOUTPREFIX])[0]
            if fanouttable[fanoutkey] == -1:
                fanouttable[fanoutkey] = location

            rawindex += struct.pack(INDEXFORMAT, namehash, offset, size)

        rawfanouttable = ''
        last = 0
        for offset in fanouttable:
            offset = offset if offset != -1 else last
            last = offset
            rawfanouttable += struct.pack(FANOUTENTRYSTRUCT, offset)

        # TODO: add version number to the index
        self.idxfp.write(rawfanouttable)
        self.idxfp.write(rawindex)
        self.idxfp.close()

class historygc(object):
    def __init__(self, repo, content, metadata):
        self.repo = repo
        self.content = content
        self.metadata = metadata

    def run(self, source, target):
        ui = self.repo.ui

        files = sorted(source.getfiles())
        count = 0
        for filename, nodes in files:
            ancestors = {}
            for node in nodes:
                ancestors.update(self.metadata.getancestors(filename, node))

            # Order the nodes children first
            orderednodes = reversed(self._toposort(ancestors))

            # Write to the pack
            dontprocess = set()
            for node in orderednodes:
                p1, p2, linknode, copyfrom = ancestors[node]

                if node in dontprocess:
                    if p1 != nullid:
                        dontprocess.add(p1)
                    if p2 != nullid:
                        dontprocess.add(p2)
                    continue

                if copyfrom:
                    dontprocess.add(p1)
                    p1 = nullid

                target.add(filename, node, p1, p2, linknode)

            count += 1
            ui.progress(_("repacking"), count, unit="files", total=len(files))

        ui.progress(_("repacking"), None)
        target.close()

    def _toposort(self, ancestors):
        def parentfunc(node):
            p1, p2, linknode, copyfrom = ancestors[node]
            parents = []
            if p1 != nullid:
                parents.append(p1)
            if p2 != nullid:
                parents.append(p2)
            return parents

        sortednodes = shallowutil.sortnodes(ancestors.keys(), parentfunc)
        return sortednodes
