# -*- coding: utf-8 -*-
'''
    :codeauthor: Pedro Algarvio (pedro@algarvio.me)

    tests.conftest
    ~~~~~~~~~~~~~~

    Prepare py.test for our test suite
'''
# pylint: disable=wrong-import-order,wrong-import-position,3rd-party-local-module-not-gated
# pylint: disable=redefined-outer-name,invalid-name

# Import python libs
from __future__ import absolute_import, print_function, unicode_literals
import os
import sys
import stat
import pprint
import shutil
import socket
import logging
import tempfile
import textwrap
from functools import partial, wraps
from contextlib import contextmanager

TESTS_DIR = os.path.dirname(os.path.normpath(os.path.abspath(__file__)))
CODE_DIR = os.path.dirname(TESTS_DIR)

# Change to code checkout directory
os.chdir(CODE_DIR)

# Make sure the current directory is the first item in sys.path
if CODE_DIR in sys.path:
    sys.path.remove(CODE_DIR)
sys.path.insert(0, CODE_DIR)

# Import test libs
from tests.support.runtests import RUNTIME_VARS
from tests.support.sminion import create_sminion, check_required_sminion_attributes
from tests.support.helpers import PRE_PYTEST_SKIP_REASON, PRE_PYTEST_SKIP_OR_NOT

# Import pytest libs
import pytest
import _pytest.logging
import _pytest.skipping
from _pytest.mark.evaluate import MarkEvaluator

# Import 3rd-party libs
import psutil
from salt.ext import six

# Import salt libs
import salt.loader
import salt.config
import salt.utils.files
import salt.utils.path
import salt.log.setup
import salt.log.mixins
import salt.utils.platform
import salt.utils.win_functions
from salt.serializers import yaml
from salt.utils.immutabletypes import freeze

# Coverage
if 'COVERAGE_PROCESS_START' in os.environ:
    MAYBE_RUN_COVERAGE = True
    COVERAGERC_FILE = os.environ['COVERAGE_PROCESS_START']
else:
    COVERAGERC_FILE = os.path.join(CODE_DIR, '.coveragerc')
    MAYBE_RUN_COVERAGE = sys.argv[0].endswith('pytest.py') or '_COVERAGE_RCFILE' in os.environ
    if MAYBE_RUN_COVERAGE:
        # Flag coverage to track suprocesses by pointing it to the right .coveragerc file
        os.environ[str('COVERAGE_PROCESS_START')] = str(COVERAGERC_FILE)

# Define the pytest plugins we rely on
pytest_plugins = ['tempdir', 'helpers_namespace', 'salt-runtests-bridge']

# Define where not to collect tests from
collect_ignore = ['setup.py']


# Patch PyTest logging handlers
class LogCaptureHandler(salt.log.mixins.ExcInfoOnLogLevelFormatMixIn,
                        logging.NullHandler):
    '''
    Subclassing PyTest's LogCaptureHandler in order to add the
    exc_info_on_loglevel functionality and actually make it a NullHandler,
    it's only used to print log messages emmited during tests, which we
    have explicitly disabled in pytest.ini
    '''


_pytest.logging.LogCaptureHandler = LogCaptureHandler


class LiveLoggingStreamHandler(salt.log.mixins.ExcInfoOnLogLevelFormatMixIn,
                               _pytest.logging._LiveLoggingStreamHandler):
    '''
    Subclassing PyTest's LiveLoggingStreamHandler in order to add the
    exc_info_on_loglevel functionality.
    '''


_pytest.logging._LiveLoggingStreamHandler = LiveLoggingStreamHandler

# Reset logging root handlers
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)


# Reset the root logger to it's default level(because salt changed it)
logging.root.setLevel(logging.WARNING)

log = logging.getLogger('salt.testsuite')


# ----- PyTest Tempdir Plugin Hooks --------------------------------------------------------------------------------->
def pytest_tempdir_basename():
    '''
    Return the temporary directory basename for the salt test suite.
    '''
    return 'salt-tests-tmpdir'
# <---- PyTest Tempdir Plugin Hooks ----------------------------------------------------------------------------------


# ----- CLI Options Setup ------------------------------------------------------------------------------------------->
def pytest_addoption(parser):
    '''
    register argparse-style options and ini-style config values.
    '''
    parser.addoption(
        '--sysinfo',
        default=False,
        action='store_true',
        help='Print some system information.'
    )
    parser.addoption(
        '--transport',
        default='zeromq',
        choices=('zeromq', 'tcp'),
        help=('Select which transport to run the integration tests with, '
              'zeromq or tcp. Default: %default')
    )
    test_selection_group = parser.getgroup('Tests Selection')
    test_selection_group.addoption(
        '--ssh',
        '--ssh-tests',
        dest='ssh',
        action='store_true',
        default=False,
        help='Run salt-ssh tests. These tests will spin up a temporary '
             'SSH server on your machine. In certain environments, this '
             'may be insecure! Default: False'
    )
    test_selection_group.addoption(
        '--proxy',
        '--proxy-tests',
        dest='proxy',
        action='store_true',
        default=False,
        help='Run proxy tests'
    )
    test_selection_group.addoption(
        '--run-destructive',
        action='store_true',
        default=False,
        help='Run destructive tests. These tests can include adding '
             'or removing users from your system for example. '
             'Default: False'
    )
    test_selection_group.addoption(
        '--run-expensive',
        action='store_true',
        default=False,
        help='Run expensive tests. These tests usually involve costs '
             'like for example bootstrapping a cloud VM. '
             'Default: False'
    )
    output_options_group = parser.getgroup('Output Options')
    output_options_group.addoption(
        '--output-columns',
        default=80,
        type=int,
        help='Number of maximum columns to use on the output'
    )
    output_options_group.addoption(
        '--no-colors',
        '--no-colours',
        default=False,
        action='store_true',
        help='Disable colour printing.'
    )

    # ----- Test Groups --------------------------------------------------------------------------------------------->
    # This will allow running the tests in chunks
    test_selection_group.addoption(
        '--test-group-count', dest='test-group-count', type=int,
        help='The number of groups to split the tests into'
    )
    test_selection_group.addoption(
        '--test-group', dest='test-group', type=int,
        help='The group of tests that should be executed'
    )
    # <---- Test Groups ----------------------------------------------------------------------------------------------
# <---- CLI Options Setup --------------------------------------------------------------------------------------------


# ----- Register Markers -------------------------------------------------------------------------------------------->
@pytest.mark.trylast
def pytest_configure(config):
    '''
    called after command line options have been parsed
    and all plugins and initial conftest files been loaded.
    '''
    for dirname in os.listdir(CODE_DIR):
        if not os.path.isdir(dirname):
            continue
        if dirname != 'tests':
            config.addinivalue_line('norecursedirs', os.path.join(CODE_DIR, dirname))

    # Expose the markers we use to pytest CLI
    config.addinivalue_line(
        'markers',
        'destructive_test: Run destructive tests. These tests can include adding '
        'or removing users from your system for example.'
    )
    config.addinivalue_line(
        'markers',
        'skip_if_not_root: Skip if the current user is not `root`.'
    )
    config.addinivalue_line(
        'markers',
        'skip_if_binaries_missing(*binaries, check_all=False, message=None): Skip if '
        'any of the passed binaries are not found in path. If \'check_all\' is '
        '\'True\', then all binaries must be found.'
    )
    config.addinivalue_line(
        'markers',
        'requires_network(only_local_network=False): Skip if no networking is set up. '
        'If \'only_local_network\' is \'True\', only the local network is checked.'
    )
    config.addinivalue_line(
        'markers',
        'requires_salt_modules(*required_module_names): Skip if at least one module is not available.'
    )
    config.addinivalue_line(
        'markers',
        'requires_salt_states(*required_state_names): Skip if at least one state module is not available.'
    )
    config.addinivalue_line(
        'markers',
        'windows_whitelisted: Mark test as whitelisted to run under Windows'
    )
    # Make sure the test suite "knows" this is a pytest test run
    RUNTIME_VARS.PYTEST_SESSION = True
# <---- Register Markers ---------------------------------------------------------------------------------------------


# ----- PyTest Tweaks ----------------------------------------------------------------------------------------------->
def set_max_open_files_limits(min_soft=3072, min_hard=4096):

    # Get current limits
    if salt.utils.platform.is_windows():
        import win32file
        prev_hard = win32file._getmaxstdio()
        prev_soft = 512
    else:
        import resource
        prev_soft, prev_hard = resource.getrlimit(resource.RLIMIT_NOFILE)

    # Check minimum required limits
    set_limits = False
    if prev_soft < min_soft:
        soft = min_soft
        set_limits = True
    else:
        soft = prev_soft

    if prev_hard < min_hard:
        hard = min_hard
        set_limits = True
    else:
        hard = prev_hard

    # Increase limits
    if set_limits:
        log.debug(
            ' * Max open files settings is too low (soft: %s, hard: %s) for running the tests. '
            'Trying to raise the limits to soft: %s, hard: %s',
            prev_soft,
            prev_hard,
            soft,
            hard
        )
        try:
            if salt.utils.platform.is_windows():
                hard = 2048 if hard > 2048 else hard
                win32file._setmaxstdio(hard)
            else:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        except Exception as err:  # pylint: disable=broad-except
            log.error(
                'Failed to raise the max open files settings -> %s. Please issue the following command '
                'on your console: \'ulimit -u %s\'',
                err,
                soft,
            )
            exit(1)
    return soft, hard


def pytest_report_header():
    soft, hard = set_max_open_files_limits()
    return 'max open files; soft: {}; hard: {}'.format(soft, hard)


#@pytest.hookimpl(hookwrapper=True, trylast=True)
def _pytest_collection_modifyitems(config, items):
    '''
    called after collection has been performed, may filter or re-order
    the items in-place.

    :param _pytest.main.Session session: the pytest session object
    :param _pytest.config.Config config: pytest config object
    :param List[_pytest.nodes.Item] items: list of item objects
    '''
    # Let PyTest or other plugins handle the initial collection
    yield
    groups_collection_modifyitems(config, items)

    log.warning('Mofifying collected tests to keep track of fixture usage')
    for item in items:
        for fixture in item.fixturenames:
            if fixture not in item._fixtureinfo.name2fixturedefs:
                continue
            for fixturedef in item._fixtureinfo.name2fixturedefs[fixture]:
                if fixturedef.scope == 'function':
                    continue
                try:
                    node_ids = fixturedef.node_ids
                except AttributeError:
                    node_ids = fixturedef.node_ids = set()
                node_ids.add(item.nodeid)
                try:
                    fixturedef.finish.__wrapped__
                except AttributeError:
                    original_func = fixturedef.finish

                    def wrapper(func, fixturedef):

                        @wraps(func)
                        def wrapped(self, request):
                            try:
                                return self._finished
                            except AttributeError:
                                if self.node_ids:
                                    log.debug(
                                        '%s is still going to be used, not terminating it. '
                                        'Still in use on:\n%s',
                                        self,
                                        pprint.pformat(list(self.node_ids))
                                    )
                                    return
                                log.debug('Finish called on %s', self)
                                try:
                                    return func(request)
                                finally:
                                    self._finished = True
                        return partial(wrapped, fixturedef)

                    fixturedef.finish = wrapper(fixturedef.finish, fixturedef)
                    try:
                        fixturedef.finish.__wrapped__
                    except AttributeError:
                        fixturedef.finish.__wrapped__ = original_func


#@pytest.hookimpl(trylast=True, hookwrapper=True)
def _pytest_runtest_protocol(item, nextitem):
    '''
    implements the runtest_setup/call/teardown protocol for
    the given test item, including capturing exceptions and calling
    reporting hooks.

    :arg item: test item for which the runtest protocol is performed.

    :arg nextitem: the scheduled-to-be-next test item (or None if this
                   is the end my friend).  This argument is passed on to
                   :py:func:`pytest_runtest_teardown`.

    :return boolean: True if no further hook implementations should be invoked.


    Stops at first non-None result, see :ref:`firstresult`
    '''
    request = item._request
    used_fixture_defs = []
    for fixture in item.fixturenames:
        if fixture not in item._fixtureinfo.name2fixturedefs:
            continue
        for fixturedef in reversed(item._fixtureinfo.name2fixturedefs[fixture]):
            if fixturedef.scope == 'function':
                continue
            used_fixture_defs.append(fixturedef)
    try:
        # Run the test
        yield
    finally:
        for fixturedef in used_fixture_defs:
            fixturedef.node_ids.remove(item.nodeid)
            if not fixturedef.node_ids:
                # This fixture is not used in any more test functions
                fixturedef.finish(request)
    del request
    del used_fixture_defs
# <---- PyTest Tweaks ------------------------------------------------------------------------------------------------


# ----- Test Setup -------------------------------------------------------------------------------------------------->
def _has_unittest_attr(item, attr):
    # XXX: This is a hack while we support both runtests.py and PyTest
    if hasattr(item.obj, attr):
        return True
    if item.cls and hasattr(item.cls, attr):
        return True
    if item.parent and hasattr(item.parent.obj, attr):
        return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    '''
    Fixtures injection based on markers or test skips based on CLI arguments
    '''
    integration_utils_tests_path = os.path.join(CODE_DIR, 'tests', 'integration', 'utils')
    if str(item.fspath).startswith(integration_utils_tests_path) and PRE_PYTEST_SKIP_OR_NOT is True:
        item._skipped_by_mark = True
        pytest.skip(PRE_PYTEST_SKIP_REASON)

    destructive_tests_marker = item.get_closest_marker('destructive_test')
    if destructive_tests_marker is not None or _has_unittest_attr(item, '__destructive_test__'):
        if item.config.getoption('--run-destructive') is False:
            item._skipped_by_mark = True
            pytest.skip('Destructive tests are disabled')

    expensive_tests_marker = item.get_closest_marker('expensive_test')
    if expensive_tests_marker is not None or _has_unittest_attr(item, '__expensive_test__'):
        if item.config.getoption('--run-expensive') is False:
            item._skipped_by_mark = True
            pytest.skip('Expensive tests are disabled')

    skip_if_not_root_marker = item.get_closest_marker('skip_if_not_root')
    if skip_if_not_root_marker is not None or _has_unittest_attr(item, '__skip_if_not_root__'):
        if not sys.platform.startswith('win'):
            if os.getuid() != 0:
                item._skipped_by_mark = True
                pytest.skip('You must be logged in as root to run this test')
        else:
            current_user = salt.utils.win_functions.get_current_user()
            if current_user != 'SYSTEM':
                if not salt.utils.win_functions.is_admin(current_user):
                    item._skipped_by_mark = True
                    pytest.skip('You must be logged in as an Administrator to run this test')

    skip_if_binaries_missing_marker = item.get_closest_marker('skip_if_binaries_missing')
    if skip_if_binaries_missing_marker is not None:
        binaries = skip_if_binaries_missing_marker.args
        if len(binaries) == 1:
            if isinstance(binaries[0], (list, tuple, set, frozenset)):
                binaries = binaries[0]
        check_all = skip_if_binaries_missing_marker.kwargs.get('check_all', False)
        message = skip_if_binaries_missing_marker.kwargs.get('message', None)
        if check_all:
            for binary in binaries:
                if salt.utils.path.which(binary) is None:
                    item._skipped_by_mark = True
                    pytest.skip(
                        '{0}The "{1}" binary was not found'.format(
                            message and '{0}. '.format(message) or '',
                            binary
                        )
                    )
        elif salt.utils.path.which_bin(binaries) is None:
            item._skipped_by_mark = True
            pytest.skip(
                '{0}None of the following binaries was found: {1}'.format(
                    message and '{0}. '.format(message) or '',
                    ', '.join(binaries)
                )
            )

    requires_network_marker = item.get_closest_marker('requires_network')
    if requires_network_marker is not None:
        only_local_network = requires_network_marker.kwargs.get('only_local_network', False)
        has_local_network = False
        # First lets try if we have a local network. Inspired in verify_socket
        try:
            pubsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            retsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            pubsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            pubsock.bind(('', 18000))
            pubsock.close()
            retsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            retsock.bind(('', 18001))
            retsock.close()
            has_local_network = True
        except socket.error:
            # I wonder if we just have IPV6 support?
            try:
                pubsock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                retsock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                pubsock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
                )
                pubsock.bind(('', 18000))
                pubsock.close()
                retsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                retsock.bind(('', 18001))
                retsock.close()
                has_local_network = True
            except socket.error:
                # Let's continue
                pass

        if only_local_network is True:
            if has_local_network is False:
                # Since we're only supposed to check local network, and no
                # local network was detected, skip the test
                item._skipped_by_mark = True
                pytest.skip('No local network was detected')

        # We are using the google.com DNS records as numerical IPs to avoid
        # DNS lookups which could greatly slow down this check
        for addr in ('173.194.41.198', '173.194.41.199', '173.194.41.200',
                     '173.194.41.201', '173.194.41.206', '173.194.41.192',
                     '173.194.41.193', '173.194.41.194', '173.194.41.195',
                     '173.194.41.196', '173.194.41.197'):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.25)
                sock.connect((addr, 80))
                sock.close()
                # We connected? Stop the loop
                break
            except socket.error:
                # Let's check the next IP
                continue
            else:
                item._skipped_by_mark = True
                pytest.skip('No internet network connection was detected')

    requires_salt_modules_marker = item.get_closest_marker('requires_salt_modules')
    if requires_salt_modules_marker is not None:
        required_salt_modules = requires_salt_modules_marker.args
        if len(required_salt_modules) == 1 and isinstance(required_salt_modules[0], (list, tuple, set)):
            required_salt_modules = required_salt_modules[0]
        required_salt_modules = set(required_salt_modules)
        not_available_modules = check_required_sminion_attributes('functions', required_salt_modules)

        if not_available_modules:
            item._skipped_by_mark = True
            if len(not_available_modules) == 1:
                pytest.skip('Salt module \'{}\' is not available'.format(*not_available_modules))
            pytest.skip('Salt modules not available: {}'.format(', '.join(not_available_modules)))

    requires_salt_states_marker = item.get_closest_marker('requires_salt_states')
    if requires_salt_states_marker is not None:
        required_salt_states = requires_salt_states_marker.args
        if len(required_salt_states) == 1 and isinstance(required_salt_states[0], (list, tuple, set)):
            required_salt_states = required_salt_states[0]
        required_salt_states = set(required_salt_states)
        not_available_states = check_required_sminion_attributes('states', required_salt_states)

        if not_available_states:
            item._skipped_by_mark = True
            if len(not_available_states) == 1:
                pytest.skip('Salt state module \'{}\' is not available'.format(*not_available_states))
            pytest.skip('Salt state modules not available: {}'.format(', '.join(not_available_states)))

    if salt.utils.platform.is_windows():
        if not item.fspath.fnmatch(os.path.join(CODE_DIR, 'tests', 'unit', '*')):
            # Unit tests are whitelisted on windows by default, so, we're only
            # after all other tests
            windows_whitelisted_marker = item.get_closest_marker('windows_whitelisted')
            if windows_whitelisted_marker is None:
                item._skipped_by_mark = True
                pytest.skip('Test is not whitelisted for Windows')
# <---- Test Setup ---------------------------------------------------------------------------------------------------


# ----- Test Groups Selection --------------------------------------------------------------------------------------->
def get_group_size(total_items, total_groups):
    '''
    Return the group size.
    '''
    return int(total_items / total_groups)


def get_group(items, group_count, group_size, group_id):
    '''
    Get the items from the passed in group based on group size.
    '''
    start = group_size * (group_id - 1)
    end = start + group_size
    total_items = len(items)

    if start >= total_items:
        pytest.fail("Invalid test-group argument. start({})>=total_items({})".format(start, total_items))
    elif start < 0:
        pytest.fail("Invalid test-group argument. Start({})<0".format(start))

    if group_count == group_id and end < total_items:
        # If this is the last group and there are still items to test
        # which don't fit in this group based on the group items count
        # add them anyway
        end = total_items

    return items[start:end]


def groups_collection_modifyitems(config, items):
    group_count = config.getoption('test-group-count')
    group_id = config.getoption('test-group')

    if not group_count or not group_id:
        # We're not selection tests using groups, don't do any filtering
        return

    total_items = len(items)

    group_size = get_group_size(total_items, group_count)
    tests_in_group = get_group(items, group_count, group_size, group_id)
    # Replace all items in the list
    items[:] = tests_in_group

    terminal_reporter = config.pluginmanager.get_plugin('terminalreporter')
    terminal_reporter.write(
        'Running test group #{0} ({1} tests)\n'.format(
            group_id,
            len(items)
        ),
        yellow=True
    )
# <---- Test Groups Selection ----------------------------------------------------------------------------------------


# ----- Pytest Helpers ---------------------------------------------------------------------------------------------->
if six.PY2:
    # backport mock_open from the python 3 unittest.mock library so that we can
    # mock read, readline, readlines, and file iteration properly

    file_spec = None

    def _iterate_read_data(read_data):
        # Helper for mock_open:
        # Retrieve lines from read_data via a generator so that separate calls to
        # readline, read, and readlines are properly interleaved
        data_as_list = ['{0}\n'.format(l) for l in read_data.split('\n')]

        if data_as_list[-1] == '\n':
            # If the last line ended in a newline, the list comprehension will have an
            # extra entry that's just a newline.  Remove this.
            data_as_list = data_as_list[:-1]
        else:
            # If there wasn't an extra newline by itself, then the file being
            # emulated doesn't have a newline to end the last line  remove the
            # newline that our naive format() added
            data_as_list[-1] = data_as_list[-1][:-1]

        for line in data_as_list:
            yield line

    @pytest.helpers.mock.register
    def mock_open(mock=None, read_data=''):
        """
        A helper function to create a mock to replace the use of `open`. It works
        for `open` called directly or used as a context manager.

        The `mock` argument is the mock object to configure. If `None` (the
        default) then a `MagicMock` will be created for you, with the API limited
        to methods or attributes available on standard file handles.

        `read_data` is a string for the `read` methoddline`, and `readlines` of the
        file handle to return.  This is an empty string by default.
        """
        _mock = pytest.importorskip('mock', minversion='2.0.0')

        def _readlines_side_effect(*args, **kwargs):
            if handle.readlines.return_value is not None:
                return handle.readlines.return_value
            return list(_data)

        def _read_side_effect(*args, **kwargs):
            if handle.read.return_value is not None:
                return handle.read.return_value
            return ''.join(_data)

        def _readline_side_effect():
            if handle.readline.return_value is not None:
                while True:
                    yield handle.readline.return_value
            for line in _data:
                yield line

        global file_spec
        if file_spec is None:
            file_spec = file  # pylint: disable=undefined-variable

        if mock is None:
            mock = _mock.MagicMock(name='open', spec=open)

        handle = _mock.MagicMock(spec=file_spec)
        handle.__enter__.return_value = handle

        _data = _iterate_read_data(read_data)

        handle.write.return_value = None
        handle.read.return_value = None
        handle.readline.return_value = None
        handle.readlines.return_value = None

        handle.read.side_effect = _read_side_effect
        handle.readline.side_effect = _readline_side_effect()
        handle.readlines.side_effect = _readlines_side_effect

        mock.return_value = handle
        return mock
else:
    @pytest.helpers.mock.register
    def mock_open(mock=None, read_data=''):
        _mock = pytest.importorskip('mock', minversion='2.0.0')
        return _mock.mock_open(mock=mock, read_data=read_data)


@pytest.helpers.register
@contextmanager
def temp_directory(name=None):
    if name is not None:
        directory_path = os.path.join(RUNTIME_VARS.TMP, name)
    else:
        directory_path = tempfile.mkdtemp(dir=RUNTIME_VARS.TMP)

    yield directory_path

    shutil.rmtree(directory_path, ignore_errors=True)


@pytest.helpers.register
@contextmanager
def temp_file(name, contents=None, directory=None, strip_first_newline=True):
    if directory is None:
        directory = RUNTIME_VARS.TMP

    file_path = os.path.join(directory, name)
    file_directory = os.path.dirname(file_path)
    if contents is not None:
        if contents:
            if contents.startswith('\n') and strip_first_newline:
                contents = contents[1:]
            file_contents = textwrap.dedent(contents)
        else:
            file_contents = contents

    try:
        if not os.path.isdir(file_directory):
            os.makedirs(file_directory)
        if contents is not None:
            with salt.utils.files.fopen(file_path, 'w') as wfh:
                wfh.write(file_contents)

        yield file_path

    finally:
        try:
            os.unlink(file_path)
        except OSError:
            # Already deleted
            pass


@pytest.helpers.register
def temp_state_file(name, contents, saltenv='base', strip_first_newline=True):

    if saltenv == 'base':
        directory = RUNTIME_VARS.TMP_STATE_TREE
    elif saltenv == 'prod':
        directory = RUNTIME_VARS.TMP_PRODENV_STATE_TREE
    else:
        raise RuntimeError('"saltenv" can only be "base" or "prod", not "{}"'.format(saltenv))
    return temp_file(name, contents, directory=directory, strip_first_newline=strip_first_newline)
# <---- Pytest Helpers -----------------------------------------------------------------------------------------------


# ----- Fixtures Overrides ------------------------------------------------------------------------------------------>
@pytest.fixture(scope="session")
def salt_factories_config():
    """
    Return a dictionary with the keyworkd arguments for SaltFactoriesManager
    """
    return {
        "executable": sys.executable,
        "code_dir": CODE_DIR,
        "inject_coverage": MAYBE_RUN_COVERAGE,
        "inject_sitecustomize": MAYBE_RUN_COVERAGE,
        "start_timeout": 30 if (os.environ.get('JENKINS_URL') or os.environ.get('CI')) else 10
    }


@pytest.fixture(scope="session")
def salt_factories(salt_factories):
    """
    Override salt_factories fixture just to patch RUNTIME_VARS
    """
    RUNTIME_VARS.TMP_SCRIPT_DIR = salt_factories.scripts_dir
    return salt_factories


# ----- Salt Configuration ------------------------------------------------------------------------------------------>
def _get_virtualenv_binary_path():
    try:
        return _get_virtualenv_binary_path.__virtualenv_binary__
    except AttributeError:
        # Under windows we can't seem to properly create a virtualenv off of another
        # virtualenv, we can on linux but we will still point to the virtualenv binary
        # outside the virtualenv running the test suite, if that's the case.
        try:
            real_prefix = sys.real_prefix
            # The above attribute exists, this is a virtualenv
            if salt.utils.platform.is_windows():
                virtualenv_binary = os.path.join(real_prefix, 'Scripts', 'virtualenv.exe')
            else:
                # We need to remove the virtualenv from PATH or we'll get the virtualenv binary
                # from within the virtualenv, we don't want that
                path = os.environ.get('PATH')
                if path is not None:
                    path_items = path.split(os.pathsep)
                    for item in path_items[:]:
                        if item.startswith(sys.base_prefix):
                            path_items.remove(item)
                    os.environ['PATH'] = os.pathsep.join(path_items)
                virtualenv_binary = salt.utils.path.which('virtualenv')
                if path is not None:
                    # Restore previous environ PATH
                    os.environ['PATH'] = path
                if not virtualenv_binary.startswith(real_prefix):
                    virtualenv_binary = None
            if virtualenv_binary and not os.path.exists(virtualenv_binary):
                # It doesn't exist?!
                virtualenv_binary = None
        except AttributeError:
            # We're not running inside a virtualenv
            virtualenv_binary = None
        _get_virtualenv_binary_path.__virtualenv_binary__ = virtualenv_binary
        return virtualenv_binary


@pytest.fixture(scope="session")
def integration_files_dir(salt_factories):
    '''
    Fixture which returns the salt integration files directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = salt_factories.root_dir.join('integration-files')
    dirname.ensure(dir=True)
    return dirname


@pytest.fixture(scope="session")
def state_tree_root_dir(integration_files_dir):
    '''
    Fixture which returns the salt state tree root directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = integration_files_dir.join('state-tree')
    dirname.ensure(dir=True)
    return dirname


@pytest.fixture(scope="session")
def pillar_tree_root_dir(integration_files_dir):
    '''
    Fixture which returns the salt pillar tree root directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = integration_files_dir.join('pillar-tree')
    dirname.ensure(dir=True)
    return dirname


@pytest.fixture(scope="session")
def base_env_state_tree_root_dir(state_tree_root_dir):
    '''
    Fixture which returns the salt base environment state tree directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = state_tree_root_dir.join('base')
    dirname.ensure(dir=True)
    RUNTIME_VARS.TMP_STATE_TREE = dirname.realpath().strpath
    return dirname


@pytest.fixture(scope="session")
def prod_env_state_tree_root_dir(state_tree_root_dir):
    '''
    Fixture which returns the salt prod environment state tree directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = state_tree_root_dir.join('prod')
    dirname.ensure(dir=True)
    RUNTIME_VARS.TMP_PRODENV_STATE_TREE = dirname.realpath().strpath
    return dirname


@pytest.fixture(scope="session")
def base_env_pillar_tree_root_dir(pillar_tree_root_dir):
    '''
    Fixture which returns the salt base environment pillar tree directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = pillar_tree_root_dir.join('base')
    dirname.ensure(dir=True)
    RUNTIME_VARS.TMP_PILLAR_TREE = dirname.realpath().strpath
    return dirname


@pytest.fixture(scope="session")
def prod_env_pillar_tree_root_dir(pillar_tree_root_dir):
    '''
    Fixture which returns the salt prod environment pillar tree directory path.
    Creates the directory if it does not yet exist.
    '''
    dirname = pillar_tree_root_dir.join('prod')
    dirname.ensure(dir=True)
    RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE = dirname.realpath().strpath
    return dirname


# @pytest.fixture(scope='session')
# def salt_syndic_master_config(request, salt_factories):
#     return salt_factories.configure_master(request, 'syndic_master', order_masters=True)


# @pytest.fixture(scope='session')
# def salt_syndic_config(request, salt_factories, salt_syndic_master_config):
#     return salt_factories.configure_syndic(request, 'syndic', master_of_masters_id='syndic_master')


# @pytest.fixture(scope='session')
# def salt_master_config(request, salt_factories, salt_syndic_master_config):
    # return salt_factories.configure_master(request, 'master', master_of_masters_id='syndic_master')

@pytest.fixture(scope='session')
def salt_master_config(request, salt_factories):
    return salt_factories.configure_master(request, 'master')


@pytest.fixture(scope='session')
def salt_minion_config(request, salt_factories, salt_master_config):
    return salt_factories.configure_minion(request, 'minion', master_id='master')


@pytest.fixture(scope='session')
def salt_sub_minion_config(request, salt_factories, salt_master_config):
    return salt_factories.configure_minion(request, 'sub_minion', master_id='master')


def pytest_saltfactories_generate_default_master_configuration(
    request, factories_manager, root_dir, master_id, order_masters
):
    """
    Hook which should return a dictionary tailored for the provided master_id

    Stops at the first non None result
    """
    if master_id == "master":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'master')) as rfh:
            opts = yaml.deserialize(rfh.read())

            tests_known_hosts_file = root_dir.join('salt_ssh_known_hosts').strpath
            with salt.utils.files.fopen(tests_known_hosts_file, 'w') as known_hosts:
                known_hosts.write('')

            opts['known_hosts_file'] = tests_known_hosts_file
            opts['syndic_master'] = 'localhost'
            opts['transport'] = request.config.getoption('--transport')

            # Config settings to test `event_return`
            if 'returner_dirs' not in opts:
                opts['returner_dirs'] = []
            opts['returner_dirs'].append(os.path.join(RUNTIME_VARS.FILES, 'returners'))
            opts['event_return'] = 'runtests_noop'

            return opts
    elif master_id == "syndic_master":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'syndic_master')) as rfh:
            opts = yaml.deserialize(rfh.read())

            opts['hosts.file'] = root_dir.join('hosts').strpath
            opts['aliases.file'] = root_dir.join('aliases').strpath
            opts['transport'] = request.config.getoption('--transport')

            return opts
    elif master_id == "mm-master":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'mm_master')) as rfh:
            opts = yaml.deserialize(rfh.read())
            return opts
    elif master_id == "mm-sub-master":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'mm_sub_master')) as rfh:
            opts = yaml.deserialize(rfh.read())
            return opts


def pytest_saltfactories_master_configuration_overrides(
    request, factories_manager, root_dir, master_id, default_options, order_masters
):
    """
    Hook which should return a dictionary tailored for the provided master_id.
    This dictionary will override the default_options dictionary.

    Stops at the first non None result
    """
    opts = {}
    ext_pillar = []
    if salt.utils.platform.is_windows():
        ext_pillar.append(
            {'cmd_yaml': 'type {0}'.format(os.path.join(RUNTIME_VARS.FILES, 'ext.yaml'))}
        )
    else:
        ext_pillar.append(
            {'cmd_yaml': 'cat {0}'.format(os.path.join(RUNTIME_VARS.FILES, 'ext.yaml'))}
        )
    if master_id == "master":
        ext_pillar.append(
            {
                'file_tree': {
                    'root_dir':  os.path.join(RUNTIME_VARS.PILLAR_DIR, 'base', 'file_tree'),
                    'follow_dir_links': False,
                    'keep_newline': True
                }
            }
        )
        opts['pillar_opts'] = True
        opts['reactor'] = [
            {
                'salt/minion/*/start': [
                    os.path.join(RUNTIME_VARS.FILES, 'reactor-sync-minion.sls')
                ],
            },
            {
                'salt/test/reactor': [
                    os.path.join(RUNTIME_VARS.FILES, 'reactor-test.sls')
                ],
            }
        ]

    # We need to copy the extension modules into the new master root_dir or
    # it will be prefixed by it
    extension_modules_path = root_dir.join('extension_modules').strpath
    if not os.path.exists(extension_modules_path):
        shutil.copytree(
            os.path.join(
                RUNTIME_VARS.FILES, 'extension_modules'
            ),
            extension_modules_path
        )

    # Copy the autosign_file to the new  master root_dir
    autosign_file_path = root_dir.join('autosign_file').strpath
    shutil.copyfile(
        os.path.join(RUNTIME_VARS.FILES, 'autosign_file'),
        autosign_file_path
    )
    # all read, only owner write
    autosign_file_permissions = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | stat.S_IWUSR
    os.chmod(autosign_file_path, autosign_file_permissions)

    opts.update(
        {
            'ext_pillar': ext_pillar,
            'extension_modules': extension_modules_path,
            'file_roots': {
                'base': [
                    RUNTIME_VARS.TMP_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'file', 'base'),
                ],
                # Alternate root to test __env__ choices
                'prod': [
                    RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'file', 'prod'),
                ]
            },
            'pillar_roots': {
                'base': [
                    RUNTIME_VARS.TMP_PILLAR_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'pillar', 'base'),
                ],
                'prod': [
                    RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE
                ]
            },
        }
    )
    return opts


def pytest_saltfactories_generate_default_minion_configuration(
    request, factories_manager, root_dir, minion_id, master_port
):
    """
    Hook which should return a dictionary tailored for the provided minion_id

    Stops at the first non None result
    """
    if minion_id == "minion":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'minion')) as rfh:
            opts = yaml.deserialize(rfh.read())
    elif minion_id == "sub_minion":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'sub_minion')) as rfh:
            opts = yaml.deserialize(rfh.read())
    elif minion_id == "mm-minion":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'mm_minion')) as rfh:
            opts = yaml.deserialize(rfh.read())
    elif minion_id == "mm-sub-minion":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'mm_sub_minion')) as rfh:
            opts = yaml.deserialize(rfh.read())
    else:
        raise RuntimeError(
            "Not prepared to handle minion_id '{}'".format(minion_id)
        )

    opts['hosts.file'] = root_dir.join('hosts').strpath
    opts['aliases.file'] = root_dir.join('aliases').strpath
    opts['transport'] = request.config.getoption('--transport')

    return opts


def pytest_saltfactories_minion_configuration_overrides(
    request, factories_manager, root_dir, minion_id, default_options
):
    """
    Hook which should return a dictionary tailored for the provided minion_id.
    This dictionary will override the default_options dictionary.

    Stops at the first non None result
    """
    if minion_id in ("minion", "sub_minion"):
        opts = {
            'file_roots': {
                'base': [
                    RUNTIME_VARS.TMP_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'file', 'base'),
                ],
                # Alternate root to test __env__ choices
                'prod': [
                    RUNTIME_VARS.TMP_PRODENV_STATE_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'file', 'prod'),
                ]
            },
            'pillar_roots': {
                'base': [
                    RUNTIME_VARS.TMP_PILLAR_TREE,
                    os.path.join(RUNTIME_VARS.FILES, 'pillar', 'base'),
                ],
                'prod': [
                    RUNTIME_VARS.TMP_PRODENV_PILLAR_TREE
                ]
            },
        }
        virtualenv_binary = _get_virtualenv_binary_path()
        if virtualenv_binary:
            opts['venv_bin'] = virtualenv_binary
        return opts
    if minion_id in ("mm-minion", "mm-sub-minion"):
        mm_master_port = factories_manager.cache["configs"]["masters"]["mm-master"]["ret_port"]
        mm_sub_master_port = factories_manager.cache["configs"]["masters"]["mm-sub-master"]["ret_port"]
        opts = {
            'master_port': '',
            'master': [
                "localhost:{}".format(mm_master_port),
                "localhost:{}".format(mm_sub_master_port)
            ],
            "test.foo": "baz"
        }
        return opts


@pytest.hookspec(firstresult=True)
def pytest_saltfactories_generate_default_syndic_configuration(
    request, factories_manager, root_dir, syndic_id, syndic_master_port
):
    """
    Hook which should return a dictionary tailored for the provided syndic_id with 3 keys:

    * `master`: The default config for the master running along with the syndic
    * `minion`: The default config for the master running along with the syndic
    * `syndic`: The default config for the master running along with the syndic

    Stops at the first non None result
    """
    factory_opts = {"master": None, "minion": None, "syndic": None}
    if syndic_id == "syndic":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'syndic')) as rfh:
            opts = yaml.deserialize(rfh.read())

            opts['hosts.file'] = root_dir.join('hosts').strpath
            opts['aliases.file'] = root_dir.join('aliases').strpath
            opts['transport'] = request.config.getoption('--transport')
            factory_opts["syndic"] = opts
    return factory_opts


@pytest.hookspec(firstresult=True)
def pytest_saltfactories_syndic_configuration_overrides(
    request, factories_manager, syndic_id, default_options
):
    """
    Hook which should return a dictionary tailored for the provided syndic_id.
    This dictionary will override the default_options dictionary.

    The returned dictionary should contain 3 keys:

    * `master`: The config overrides for the master running along with the syndic
    * `minion`: The config overrides for the master running along with the syndic
    * `syndic`: The config overridess for the master running along with the syndic

    The `default_options` parameter be None or have 3 keys, `master`, `minion`, `syndic`,
    while will contain the default options for each of the daemons.

    Stops at the first non None result
    """


@pytest.hookspec(firstresult=True)
def pytest_saltfactories_generate_default_proxy_minion_configuration(
    request, factories_manager, proxy_minion_id, master_port
):
    """
    Hook which should return a dictionary tailored for the provided proxy_minion_id

    Stops at the first non None result
    """


@pytest.hookspec(firstresult=True)
def pytest_saltfactories_proxy_minion_configuration_overrides(
    request, factories_manager, root_dir, proxy_minion_id, default_options
):
    """
    Hook which should return a dictionary tailored for the provided proxy_minion_id.
    This dictionary will override the default_options dictionary.

    Stops at the first non None result
    """
    if proxy_minion_id == "proxy":
        with salt.utils.files.fopen(os.path.join(RUNTIME_VARS.CONF_DIR, 'proxy')) as rfh:
            opts = yaml.deserialize(rfh.read())
    else:
        raise RuntimeError(
            "Not prepared to handle proxy minion_id '{}'".format(proxy_minion_id)
        )

    opts['hosts.file'] = root_dir.join('hosts').strpath
    opts['aliases.file'] = root_dir.join('aliases').strpath
    opts['transport'] = request.config.getoption('--transport')

    return opts


@pytest.fixture(scope='session', autouse=True)
def bridge_pytest_and_runtests(reap_stray_processes,
                               base_env_state_tree_root_dir,
                               prod_env_state_tree_root_dir,
                               base_env_pillar_tree_root_dir,
                               prod_env_pillar_tree_root_dir,
                               salt_factories,
                               # salt_syndic_master_config,
                               # salt_syndic_config,
                               salt_master_config,
                               salt_minion_config,
                               salt_sub_minion_config):
    # Make sure unittest2 uses the pytest generated configuration
    RUNTIME_VARS.RUNTIME_CONFIGS['master'] = freeze(salt_master_config)
    RUNTIME_VARS.RUNTIME_CONFIGS['minion'] = freeze(salt_minion_config)
    RUNTIME_VARS.RUNTIME_CONFIGS['sub_minion'] = freeze(salt_sub_minion_config)
    # RUNTIME_VARS.RUNTIME_CONFIGS['syndic_master'] = freeze(salt_syndic_master_config)
    # RUNTIME_VARS.RUNTIME_CONFIGS['syndic'] = freeze(salt_syndic_config)
    RUNTIME_VARS.RUNTIME_CONFIGS['client_config'] = freeze(salt.config.client_config(salt_master_config['conf_file']))

    # Make sure unittest2 classes know their paths
    RUNTIME_VARS.TMP_ROOT_DIR = salt_factories.root_dir.realpath().strpath
    RUNTIME_VARS.TMP_CONF_DIR = os.path.dirname(salt_master_config['conf_file'])
    RUNTIME_VARS.TMP_MINION_CONF_DIR = os.path.dirname(salt_minion_config['conf_file'])
    RUNTIME_VARS.TMP_SUB_MINION_CONF_DIR = os.path.dirname(salt_sub_minion_config['conf_file'])
    # RUNTIME_VARS.TMP_SYNDIC_MASTER_CONF_DIR = os.path.dirname(salt_syndic_master_config['conf_file'])
    # RUNTIME_VARS.TMP_SYNDIC_MINION_CONF_DIR = os.path.dirname(salt_syndic_config['conf_file'])
# <---- Salt Configuration -------------------------------------------------------------------------------------------
# <---- Fixtures Overrides -------------------------------------------------------------------------------------------


# ----- Custom Grains Mark Evaluator -------------------------------------------------------------------------------->
class GrainsMarkEvaluator(MarkEvaluator):
    _cached_grains = None

    def _getglobals(self):
        item_globals = super(GrainsMarkEvaluator, self)._getglobals()
        if GrainsMarkEvaluator._cached_grains is None:
            sminion = create_sminion()
            GrainsMarkEvaluator._cached_grains = sminion.opts['grains'].copy()
        item_globals['grains'] = GrainsMarkEvaluator._cached_grains.copy()
        return item_globals


# Patch PyTest's skipping MarkEvaluator to use our GrainsMarkEvaluator
_pytest.skipping.MarkEvaluator = GrainsMarkEvaluator
# <---- Custom Grains Mark Evaluator ---------------------------------------------------------------------------------


# ----- Custom Fixtures --------------------------------------------------------------------------------------------->
@pytest.fixture(scope='session')
def reap_stray_processes():
    # Run tests
    yield

    children = psutil.Process(os.getpid()).children(recursive=True)
    if not children:
        log.info('No astray processes found')
        return

    def on_terminate(proc):
        log.debug('Process %s terminated with exit code %s', proc, proc.returncode)

    if children:
        # Reverse the order, sublings first, parents after
        children.reverse()
        log.warning(
            'Test suite left %d astray processes running. Killing those processes:\n%s',
            len(children),
            pprint.pformat(children)
        )

        _, alive = psutil.wait_procs(children, timeout=3, callback=on_terminate)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                continue

        _, alive = psutil.wait_procs(alive, timeout=3, callback=on_terminate)
        if alive:
            # Give up
            for child in alive:
                log.warning('Process %s survived SIGKILL, giving up:\n%s', child, pprint.pformat(child.as_dict()))


@pytest.fixture(scope='session')
def grains(request):
    sminion = create_sminion()
    return sminion.opts['grains'].copy()
# <---- Custom Fixtures ----------------------------------------------------------------------------------------------
