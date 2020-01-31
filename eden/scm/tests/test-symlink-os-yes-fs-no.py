from __future__ import absolute_import

import os
import sys
import time

from edenscm.mercurial import commands, fscap, hg, ui as uimod, util
from hghave import require


require(["py2"])


TESTDIR = os.environ["TESTDIR"]
BUNDLEPATH = os.path.join(TESTDIR, "bundles", "test-no-symlinks.hg")

# only makes sense to test on os which supports symlinks
if not getattr(os, "symlink", False):
    sys.exit(80)  # SKIPPED_STATUS defined in run-tests.py

with open(os.environ["HGRCPATH"], "a+") as f:
    f.write(b"[extensions]\n")
    f.write(b"treemanifest=!\n")

u = uimod.ui.load()
# hide outer repo
hg.peer(u, {}, ".", create=True)

# clone with symlink support
hg.clone(u, {}, BUNDLEPATH, "test0")

repo = hg.repository(u, "test0")

# wait a bit, or the status call wont update the dirstate
time.sleep(1)
commands.status(u, repo)

# now disable symlink support -- this is what os.symlink would do on a
# non-symlink file system
def symlink_failure(src, dst):
    raise OSError(1, "Operation not permitted")


os.symlink = symlink_failure
fscap.getfscap = lambda *args: None


def islink_failure(path):
    return False


os.path.islink = islink_failure

# dereference links as if a Samba server has exported this to a
# Windows client
for f in "test0/a.lnk", "test0/d/b.lnk":
    os.unlink(f)
    fp = open(f, "wb")
    fp.write(util.readfile(f[:-4]))
    fp.close()

# reload repository
u = uimod.ui.load()
repo = hg.repository(u, "test0")
commands.status(u, repo)

# try cloning a repo which contains symlinks
u = uimod.ui.load()
hg.clone(u, {}, BUNDLEPATH, "test1")
