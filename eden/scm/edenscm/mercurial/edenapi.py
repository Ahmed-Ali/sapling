# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

from __future__ import absolute_import

from bindings import edenapi


def getclient(ui):
    """Obtain the edenapi client"""
    correlator = ui.correlator()
    return edenapi.client(ui._rcfg._rcfg, ui, correlator)
