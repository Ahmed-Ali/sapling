# fbhistedit.py - improved amend functionality
#
# Copyright 2014 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.
"""extends the existing histedit functionality

Adds a s/stop verb to histedit to stop after a commit was picked.
"""

from hgext import histedit
from mercurial import cmdutil
from mercurial import error
from mercurial import extensions
from mercurial import hg
from mercurial import lock
from mercurial import util
from mercurial.i18n import _

cmdtable = {}
command = cmdutil.command(cmdtable)

testedwith = 'internal'

def stop(ui, state, ha, opts):
    repo, ctx = state.repo, state.parentctx
    oldctx = repo[ha]

    hg.update(repo, ctx.node())
    stats = histedit.applychanges(ui, repo, oldctx, opts)
    if stats and stats[3] > 0:
        raise error.InterventionRequired(
            _('Fix up the change and run hg histedit --continue'))

    commit = histedit.commitfuncfor(repo, oldctx)
    new = commit(text=oldctx.description(), user=oldctx.user(),
            date=oldctx.date(), extra=oldctx.extra())

    raise error.InterventionRequired(
        _('Changes commited as %s. You may amend the commit now.\n'
          'When you are finished, run hg histedit --continue to resume') %
        repo[new])

def execute(ui, state, cmd, opts):
    repo, ctx = state.repo, state.parentctx
    hg.update(repo, ctx.node())

    # release locks so the programm can call hg and then relock.
    lock.release(state.lock, state.wlock)

    try:
        rc = util.system(cmd, environ={'HGNODE': ctx.hex()}, cwd=repo.root)
    except OSError as os:
        raise error.InterventionRequired(
            _("Cannot execute command '%s': %s") % (cmd, os))
    finally:
        # relock the repository
        state.wlock = repo.wlock()
        state.lock = repo.lock()
        repo.invalidate()
        repo.invalidatedirstate()

    if rc != 0:
        raise error.InterventionRequired(
            _("Command '%s' failed with exit status %d") % (cmd, rc))

    if util.any(repo.status()[:4]):
        raise error.InterventionRequired(
            _('Fix up the change and run hg histedit --continue'))

    newctx = repo['.']
    if ctx.node() != newctx.node():
        return newctx, [(ctx.node(), (newctx.node(),))]
    return newctx, []

# HACK:
# The following function verifyrules and bootstrap continue are copied from
# histedit.py as we have no proper way of fixing up the x/exec specialcase.
def verifyrules(orig, rules, repo, ctxs):
    """Verify that there exists exactly one edit rule per given changeset.

    Will abort if there are to many or too few rules, a malformed rule,
    or a rule on a changeset outside of the user-given range.
    """
    parsed = []
    expected = set(str(c) for c in ctxs)
    seen = set()
    for r in rules:
        if ' ' not in r:
            raise util.Abort(_('malformed line "%s"') % r)
        action, rest = r.split(' ', 1)
        # Our x/exec specialcasing
        if action in ['x', 'exec']:
            parsed.append([action, rest])
        else:
            ha = rest.strip().split(' ', 1)[0]
            try:
                ha = str(repo[ha])  # ensure its a short hash
            except error.RepoError:
                raise util.Abort(_('unknown changeset %s listed') % ha)
            if ha not in expected:
                raise util.Abort(
                    _('may not use changesets other than the ones listed'))
            if ha in seen:
                raise util.Abort(_('duplicated command for changeset %s') % ha)
            seen.add(ha)
            if action not in histedit.actiontable:
                raise util.Abort(_('unknown action "%s"') % action)
            parsed.append([action, ha])
    missing = sorted(expected - seen)  # sort to stabilize output
    if missing:
        raise util.Abort(_('missing rules for changeset %s') % missing[0],
                         hint=_('do you want to use the drop action?'))
    return parsed

# copied from mercurial histedit.py
def bootstrapcontinue(orig, ui, state, opts):
    repo, parentctx = state.repo, state.parentctx
    action, currentnode = state.rules.pop(0)
    ctx = repo['.']

    # Our x/exec specialcasing
    if action in ['x', 'exec']:
        action, currentnode = state.rules.pop(0)
        if action not in ['x', 'exec']:
            ctx = repo[currentnode]
            hg.update(repo, ctx)
    else:
        ctx = repo[currentnode]

    newchildren = histedit.gatherchildren(repo, parentctx)

    # Commit dirty working directory if necessary
    new = None
    m, a, r, d = repo.status()[:4]
    if m or a or r or d:
        # prepare the message for the commit to comes
        if action in ('f', 'fold', 'r', 'roll'):
            message = 'fold-temp-revision %s' % currentnode
        else:
            message = ctx.description()
        editopt = action in ('e', 'edit', 'm', 'mess')
        canonaction = {'e': 'edit', 'm': 'mess', 'p': 'pick'}
        editform = 'histedit.%s' % canonaction.get(action, action)
        editor = cmdutil.getcommiteditor(edit=editopt, editform=editform)
        commit = histedit.commitfuncfor(repo, ctx)
        new = commit(text=message, user=ctx.user(),
                     date=ctx.date(), extra=ctx.extra(),
                     editor=editor)
        if new is not None:
            newchildren.append(new)

    replacements = []
    # track replacements
    if ctx.node() not in newchildren:
        # note: new children may be empty when the changeset is dropped.
        # this happen e.g during conflicting pick where we revert content
        # to parent.
        replacements.append((ctx.node(), tuple(newchildren)))

    if action in ('f', 'fold', 'r', 'roll'):
        if newchildren:
            # finalize fold operation if applicable
            if new is None:
                new = newchildren[-1]
            else:
                newchildren.pop()  # remove new from internal changes
            foldopts = opts
            if action in ('r', 'roll'):
                foldopts = foldopts.copy()
                foldopts['rollup'] = True
            parentctx, repl = histedit.finishfold(ui, repo, parentctx, ctx, new,
                                         foldopts, newchildren)
            replacements.extend(repl)
        else:
            # newchildren is empty if the fold did not result in any commit
            # this happen when all folded change are discarded during the
            # merge.
            replacements.append((ctx.node(), (parentctx.node(),)))
    elif newchildren:
        # otherwise update "parentctx" before proceeding to further operation
        parentctx = repo[newchildren[-1]]

    state.parentctx = parentctx
    state.replacements.extend(replacements)

    return state

def extsetup(ui):
    histedit.editcomment = _("""# Edit history between %s and %s
#
# Commits are listed from least to most recent
#
# Commands:
#  p, pick = use commit
#  e, edit = use commit, but stop for amending
#  s, stop = use commit, and stop after committing changes
#  f, fold = use commit, but combine it with the one above
#  r, roll = like fold, but discard this commit's description
#  d, drop = remove commit from history
#  m, mess = edit message without changing commit content
#  x, exec = execute given command
#
    """)
    histedit.actiontable['s'] = stop
    histedit.actiontable['stop'] = stop
    histedit.actiontable['x'] = execute
    histedit.actiontable['exec'] = execute

    extensions.wrapfunction(histedit, 'bootstrapcontinue', bootstrapcontinue)
    extensions.wrapfunction(histedit, 'verifyrules', verifyrules)
