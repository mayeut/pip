"""Generate and work with PEP 425 Compatibility Tags."""
from __future__ import absolute_import

import re
import sys
import warnings
import platform
import logging
import ctypes

try:
    import sysconfig
except ImportError:  # pragma nocover
    # Python < 2.7
    import distutils.sysconfig as sysconfig
import distutils.util


logger = logging.getLogger(__name__)


_osx_arch_pat = re.compile(r'(.+)_(\d+)_(\d+)_(.+)')


def get_config_var(var):
    try:
        return sysconfig.get_config_var(var)
    except IOError as e:  # Issue #1074
        warnings.warn("{0}".format(e), RuntimeWarning)
        return None


def get_abbr_impl():
    """Return abbreviated implementation name."""
    if hasattr(sys, 'pypy_version_info'):
        pyimpl = 'pp'
    elif sys.platform.startswith('java'):
        pyimpl = 'jy'
    elif sys.platform == 'cli':
        pyimpl = 'ip'
    else:
        pyimpl = 'cp'
    return pyimpl


def get_impl_ver():
    """Return implementation version."""
    impl_ver = get_config_var("py_version_nodot")
    if not impl_ver or get_abbr_impl() == 'pp':
        impl_ver = ''.join(map(str, get_impl_version_info()))
    return impl_ver


def get_impl_version_info():
    """Return sys.version_info-like tuple for use in decrementing the minor
    version."""
    if get_abbr_impl() == 'pp':
        # as per https://github.com/pypa/pip/issues/2882
        return (sys.version_info[0], sys.pypy_version_info.major,
                sys.pypy_version_info.minor)
    else:
        return sys.version_info[0], sys.version_info[1]


def get_impl_tag():
    """
    Returns the Tag for this specific implementation.
    """
    return "{0}{1}".format(get_abbr_impl(), get_impl_ver())


def get_flag(var, fallback, expected=True, warn=True):
    """Use a fallback method for determining SOABI flags if the needed config
    var is unset or unavailable."""
    val = get_config_var(var)
    if val is None:
        if warn:
            logger.debug("Config variable '%s' is unset, Python ABI tag may "
                         "be incorrect", var)
        return fallback()
    return val == expected


def get_abi_tag():
    """Return the ABI tag based on SOABI (if available) or emulate SOABI
    (CPython 2, PyPy)."""
    soabi = get_config_var('SOABI')
    impl = get_abbr_impl()
    if not soabi and impl in ('cp', 'pp') and hasattr(sys, 'maxunicode'):
        d = ''
        m = ''
        u = ''
        if get_flag('Py_DEBUG',
                    lambda: hasattr(sys, 'gettotalrefcount'),
                    warn=(impl == 'cp')):
            d = 'd'
        if get_flag('WITH_PYMALLOC',
                    lambda: impl == 'cp',
                    warn=(impl == 'cp')):
            m = 'm'
        if get_flag('Py_UNICODE_SIZE',
                    lambda: sys.maxunicode == 0x10ffff,
                    expected=4,
                    warn=(impl == 'cp' and
                          sys.version_info < (3, 3))) \
                and sys.version_info < (3, 3):
            u = 'u'
        abi = '%s%s%s%s%s' % (impl, get_impl_ver(), d, m, u)
    elif soabi and soabi.startswith('cpython-'):
        abi = 'cp' + soabi.split('-')[1]
    elif soabi:
        abi = soabi.replace('.', '_').replace('-', '_')
    else:
        abi = None
    return abi


def get_platform():
    """Return our platform name 'win32', 'linux_x86_64'"""
    if sys.platform == 'darwin':
        # distutils.util.get_platform() returns the release based on the value
        # of MACOSX_DEPLOYMENT_TARGET on which Python was built, which may
        # be signficantly older than the user's current machine.
        release, _, machine = platform.mac_ver()
        split_ver = release.split('.')
        return 'macosx_{0}_{1}_{2}'.format(split_ver[0], split_ver[1], machine)
    # XXX remove distutils dependency
    result = distutils.util.get_platform().replace('.', '_').replace('-', '_')
    if result == "linux_x86_64" and sys.maxsize == 2147483647:
        # 32 bit Python program (running on a 64 bit Linux): pip should only
        # install and run 32 bit compiled extensions in that case.
        result = "linux_i686"
    return result


def is_manylinux1_compatible():
    # Only Linux, and only x86-64 / i686
    if get_platform() not in ("linux_x86_64", "linux_i686"):
        return False

    # Check for presence of _manylinux module
    try:
        import _manylinux
        return bool(_manylinux.manylinux1_compatible)
    except (ImportError, AttributeError):
        # Fall through to heuristic check below
        pass

    # Check glibc version. CentOS 5 uses glibc 2.5.
    return have_compatible_glibc(2, 5)


def have_compatible_glibc(major, minimum_minor):
    # ctypes.CDLL(None) internally calls dlopen(NULL), and as the dlopen
    # manpage says, "If filename is NULL, then the returned handle is for the
    # main program". This way we can let the linker do the work to figure out
    # which libc our process is actually using.
    process_namespace = ctypes.CDLL(None)
    try:
        gnu_get_libc_version = process_namespace.gnu_get_libc_version
    except AttributeError:
        # Symbol doesn't exist -> therefore, we are not linked to
        # glibc.
        return False

    # Call gnu_get_libc_version, which returns a string like "2.5".
    gnu_get_libc_version.restype = ctypes.c_char_p
    version_str = gnu_get_libc_version()
    # py2 / py3 compatibility:
    if not isinstance(version_str, str):
        version_str = version_str.decode("ascii")

    # Parse string and check against requested version.
    version = [int(piece) for piece in version_str.split(".")]
    if len(version) < 2:
        warnings.warn("Expected glibc version with 2 components major.minor,"
                      " got: %s" % version_str, RuntimeWarning)
        return False
    return version[0] == major and version[1] >= minimum_minor


def get_darwin_arches(major, minor, machine):
    """Return a list of supported arches (including group arches) for
    the given major, minor and machine architecture of an OS X machine.
    """
    arches = []

    def _supports_arch(major, minor, arch):
        if arch == 'ppc':
            return (major, minor) <= (10, 5)
        if arch == 'ppc64':
            return (major, minor) >= (10, 2) and (major, minor) <= (10, 5)
        if arch == 'i386':
            return (major, minor) >= (10, 4) and (major, minor) <= (10, 6)
        if arch == 'x86_64':
            return (major, minor) >= (10, 4)
        if arch in groups:
            for garch in groups[arch]:
                if _supports_arch(major, minor, garch):
                    return True
        return False

    groups = {'fat': ('i386', 'ppc'),
              'intel': ('x86_64', 'i386'),
              'fat64': ('x86_64', 'ppc64'),
              'fat32': ('x86_64', 'i386', 'ppc')
              }
    if _supports_arch(major, minor, machine):
        arches.append(machine)
    for garch in groups:
        if machine in groups[garch] and _supports_arch(major, minor, garch):
            arches.append(garch)
    arches.append('universal')

    return arches


def get_supported(versions=None, noarch=False):
    """Return a list of supported tags for each version specified in
    `versions`.

    :param versions: a list of string versions, of the form ["33", "32"],
        or None. The first version will be assumed to support our ABI.
    """
    supported = []

    # Versions must be given with respect to the preference
    if versions is None:
        versions = []
        version_info = get_impl_version_info()
        major = version_info[:-1]
        # Support all previous minor Python versions.
        for minor in range(version_info[-1], -1, -1):
            versions.append(''.join(map(str, major + (minor,))))

    impl = get_abbr_impl()

    abis = []

    abi = get_abi_tag()
    if abi:
        abis[0:0] = [abi]

    abi3s = set()
    import imp
    for suffix in imp.get_suffixes():
        if suffix[0].startswith('.abi'):
            abi3s.add(suffix[0].split('.', 2)[1])

    abis.extend(sorted(list(abi3s)))

    abis.append('none')

    if not noarch:
        arch = get_platform()
        if sys.platform == 'darwin':
            # support macosx-10.6-intel on macosx-10.9-x86_64
            match = _osx_arch_pat.match(arch)
            if match:
                name, major, minor, actual_arch = match.groups()
                tpl = '{0}_{1}_%i_%s'.format(name, major)
                arches = []
                for m in reversed(range(int(minor) + 1)):
                    for a in get_darwin_arches(int(major), m, actual_arch):
                        arches.append(tpl % (m, a))
            else:
                # arch pattern didn't match (?!)
                arches = [arch]
        elif is_manylinux1_compatible():
            arches = [arch.replace('linux', 'manylinux1'), arch]
        else:
            arches = [arch]

        # Current version, current API (built specifically for our Python):
        for abi in abis:
            for arch in arches:
                supported.append(('%s%s' % (impl, versions[0]), abi, arch))

        # Has binaries, does not use the Python API:
        for arch in arches:
            supported.append(('py%s' % (versions[0][0]), 'none', arch))

    # No abi / arch, but requires our implementation:
    supported.append(('%s%s' % (impl, versions[0]), 'none', 'any'))
    # Tagged specifically as being cross-version compatible
    # (with just the major version specified)
    supported.append(('%s%s' % (impl, versions[0][0]), 'none', 'any'))

    # No abi / arch, generic Python
    for i, version in enumerate(versions):
        supported.append(('py%s' % (version,), 'none', 'any'))
        if i == 0:
            supported.append(('py%s' % (version[0]), 'none', 'any'))

    return supported

supported_tags = get_supported()
supported_tags_noarch = get_supported(noarch=True)

implementation_tag = get_impl_tag()
