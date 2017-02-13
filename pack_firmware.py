#!/usr/bin/env python
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Packages firmware images into an executale "shell-ball".
It requires:
 - at least one firmware image (*.bin, should be AP or EC or ...)
 - pack_dist/updater.sh main script
 - pack_stub as the template/stub script for output
 - any other additional files used by updater.sh in pack_dist folder
"""

import argparse
import codecs
import md5
import os
import re
import shutil
import sys
import tempfile
import uu

from chromite.lib import cros_build_lib
import chromite.lib.cros_logging as logging

class PackError(Exception):
  pass

class PackFirmware:
  """Handles building a shell-ball firmware update.

  Private members:
    _args: Parsed arguments.
    _pack_dist: Path to 'pack_dist' directory.
    _script_base: Base directory with useful files (src/platform/firmware).
    _stub_file: Path to 'pack_stub'.
    _tmpbase: Base temporary directory.
    _tmp_dirs: List of temporary directories created.
    _versions: Collected version information, as a string.
  """
  def __init__(self, progname):
    self._script_base = os.path.dirname(progname)
    self._stub_file = os.path.join(self._script_base, 'pack_stub')
    self._pack_dist = os.path.join(self._script_base, 'pack_dist')
    self._tmp_dirs = []
    self._versions = ''
 
  def ParseArgs(self, argv):
    """Parse the available arguments.

    Invalid arguments or -h cause this function to print a message and exit.

    Args:
      argv: List of string arguments (excluding program name / argv[0])

    Returns:
      argparse.Namespace object containing the attributes.
    """
    parser = argparse.ArgumentParser(
        description='Produce a firmware update shell-ball')
    parser.add_argument('-b', '--bios_image', type=str,
                        help='Path of input AP (BIOS) firmware image')
    parser.add_argument('-w', '--bios_rw_image', type=str,
                        help='Path of input BIOS RW firmware image')
    parser.add_argument('-e', '--ec_image', type=str,
                        help='Path of input Embedded Controller firmware image')
    parser.add_argument('-p', '--pd_image', type=str,
                        help='Path of input Power Delivery firmware image')
    parser.add_argument('--script', type=str, default='updater.sh',
                        help='File name of main script file')
    parser.add_argument('-o', '--output', type=str,
                        help='Path of output filename')
    parser.add_argument(
        '--extra', type=str,
        help='Directory list (separated by :) of files to be merged')

    parser.add_argument('--remove_inactive_updaters', action='store_true',
                        help='Remove inactive updater scripts')
    parser.add_argument('--create_bios_rw_image', action='store_true',
                        help='Resign and generate a BIOS RW image')
    merge_parser = parser.add_mutually_exclusive_group(required=False)
    merge_parser.add_argument(
        '--merge_bios_rw_image', default=True, action='store_true',
        help='Merge the --bios_rw_image into --bios_image RW sections')
    merge_parser.add_argument('--no-merge_bios_rw_image', action='store_false',
                        dest='merge_bios_rw_image',
                        help='Resign and generate a BIOS RW image')

    # stable settings
    parser.add_argument('--stable_main_version', type=str,
                        help='Version of stable main firmware')
    parser.add_argument('--stable_ec_version', type=str,
                        help='Version of stable EC firmware')
    parser.add_argument('--stable_pd_version', type=str,
                        help='Version of stable PD firmware')

    # embedded tools
    parser.add_argument('--tools', type=str,
        default='flashrom mosys crossystem gbb_utility vpd dump_fmap',
        help='List of tool programs to be bundled into updater')

    # TODO(sjg@chromium.org: Consider making this accumulate rather than using
    # the ':' separator.
    parser.add_argument(
        '--tool_base', type=str, default='',
        help='Default source locations for tools programs (delimited by colon)')
    return parser.parse_args(argv)

  def _EnsureCommand(self, cmd, package):
    """Ennsure that a command is available, raising an exception if not.

    Args:
      cmd: Command to check (just the name, not the full path.
    Raises:
      PackError if the command is not available.
    """
    result = cros_build_lib.RunCommand('type %s' % cmd, shell=True, quiet=True,
                                       error_code_ok=True)
    if result.returncode:
      raise PackError("You need '%s' (package '%s')" % (cmd, package))

  def _FindTool(self, tool):
    """Find a tool in the tool_base path list, raising an exception if missing.

    Args:
      tool: Name pf tool to find (just the name, not the full path.
    Raises:
      PackError if the tool is not available.
    """
    for path in self._args.tool_base.split(':'):
      fname = os.path.join(path, tool)
      if os.path.exists(fname):
        return os.path.abspath(fname)
    raise PackError("Cannot find tool program '%s' to bundle" % tool)

  def _EnsureTools(self, tools):
    """Ensure that all required tools are available.

    Args:
      tools: List of tools to check.
    Raises:
      PackError if any tool is not available.
    """
    for tool in tools:
      self._FindTool(tool)

  def _GetTmpdir(self):
    """Get a temporary directory, and remember it for later removal.

    Returns:
      Path name of temporary directory.
    """
    fname = tempfile.mkdtemp('pack_firmware-%d' % os.getpid())
    self._tmp_dirs.append(fname)
    return fname

  def _RemoveTmpdirs(self):
    """Remove all the temporary directories"""
    for fname in self._tmp_dirs:
      shutil.rmtree(fname)
    self._tmpdirs = []

  def _AddVersion(self, name, version_string):
    if name:
      self._versions += name + ': '
    else:
      self._versions += ' '
    self._versions += '\n' + version_string

  def _AddFlashromVersion(self):
    flashrom = self._FindTool('flashrom')
    with open(flashrom, 'rb') as fd:
      data = fd.read()
      end = data.find('UTC\0')
      pos = end
      while data[pos - 1] >= ' ' and data[pos - 1] < chr(127):
        pos -= 1
      version = data[pos:end + 3]
    hash = md5.new()
    hash.update(data)
    result = cros_build_lib.RunCommand(['file', '-b', flashrom], quiet=True)
    self._AddVersion('flashrom(8)', '%s %s\n %s\n %s' %
        (hash.hexdigest(), flashrom, result.output, version))

  #with open(os.path.join(self._tmpbase, 'VERSION'), 'w'):

  def Start(self, argv):
    """Handle the creation of a firmware shell-ball.

    argv: List of arguments (excluding the program name/argv[0]).

    Raises:
      PackError if any error occurs.
    """
    self._args = self.ParseArgs(argv)
    main_script = os.path.join(self._pack_dist, self._args.script)

    self._EnsureCommand('shar', 'sharutils')
    for fname in [main_script, self._stub_file]:
      if not os.path.exists(fname):
        raise PackError("Cannot find required file '%s'" % fname)
    self._EnsureTools(self._args.tools.split())
    if (not self._args.bios_image and not self._args.ec_image and
        not self._args.pd_image):
      raise PackError('Must assign at least one of BIOS or EC or PD image')
    try:
      self._tmpbase = self._GetTmpdir()
      self._AddFlashromVersion()
      #self._CopyFirmwareFiles()
    finally:
      self._RemoveTmpdirs()

# The style guide says that we cannot pass in sys.argv[0]. That makes testing
# a pain, so this is a full argv.
def main(argv):
  pack = PackFirmware(argv[0])
  pack.Start(argv[1:])

if __name__ == "__main__":
  if not main(sys.argv):
    sys.exit(1)
