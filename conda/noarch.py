import os
from os.path import dirname, exists, isdir, join
import sys
import shutil
from subprocess import call
from json import load

from conda.base.context import context
from conda.compat import itervalues

PY_TMPL = """\
if __name__ == '__main__':
    import sys
    import %(module)s

    sys.exit(%(module)s.%(func)s())
"""


def get_noarch_cls(noarch_type):
    return NOARCH_CLASSES.get(str(noarch_type).lower(), NoArch) if noarch_type else None


def link_package(src, dst):
    try:
        os.link(src, dst)
        # on Windows os.link raises AttributeError
    except (OSError, AttributeError):
        shutil.copy2(src, dst)


def unlink_package(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def get_python_version_for_prefix(prefix):
    from conda.install import linked_data
    record = next((record for record in itervalues(linked_data(prefix)) if
                   record.name == 'python'), None)
    if record is not None:
        return record.version[:3]
    raise RuntimeError(
        "No python version found in %s. Python required to install noarch package" % prefix
    )


def get_site_packages_dir(prefix):
    if sys.platform == 'win32':
        return join(prefix, 'Lib')
    else:
        return join(prefix, 'lib/python%s' % get_python_version_for_prefix(prefix))


def get_bin_dir(prefix):
    if sys.platform == 'win32':
        return join(prefix, 'Scripts')
    else:
        return join(prefix, 'bin')


def link_files(prefix, src_root, dst_root, files, src_dir):
    dst_files = []
    for f in files:
        src = join(src_dir, src_root, f)
        dst = join(prefix, dst_root, f)
        dst_dir = dirname(dst)
        dst_files.append(dst)
        if not isdir(dst_dir):
            os.makedirs(dst_dir)
        if exists(dst):
            unlink_package(dst)

        link_package(src, dst)
    return dst_files


def compile_missing_pyc(prefix, files, cwd):
    compile_files = []
    for fn in files:
        # omit files in Library/bin, Scripts, and the root prefix - they are not generally imported
        if sys.platform == 'win32':
            if any([fn.lower().startswith(start) for start in ['library/bin', 'library\\bin',
                                                               'scripts']]):
                continue
        else:
            if fn.startswith('bin'):
                continue
        cache_prefix = ("__pycache__" + os.sep) if get_python_version_for_prefix(prefix)[0] == '3' else ""
        if (fn.endswith(".py") and
                os.path.dirname(fn) + cache_prefix + os.path.basename(fn) + 'c' not in files):
            compile_files.append(fn)

    if compile_files:
        print('compiling .pyc files...')
        for f in compile_files:
            call(["python", '-Wi', '-m', 'py_compile', f], cwd=cwd)


def create_entry_points(src_dir, bin_dir, prefix):
    from conda.install import linked
    if sys.platform == 'win32':
        python_path = join(prefix, "python.exe")
    else:
        python_path = join(prefix, "bin/python")
    get_module = lambda point: point[point.find("= ") + 1:point.find(":")].strip()
    get_func = lambda point: point[point.find(":") + 1:]
    get_cmd = lambda point: point[:point.find("= ")].strip()
    with open(join(src_dir, "info/noarch.json"), "r") as noarch_file:
        entry_points = load(noarch_file)["entry_points"]

    for entry_point in entry_points:
        path = join(bin_dir, get_cmd(entry_point))
        pyscript = PY_TMPL % {'module': get_module(entry_point), 'func': get_func(entry_point)}

        if sys.platform == 'win32':
            with open(path + '-script.py', 'w') as fo:
                packages = linked(prefix)
                packages_names = (pkg.split('-')[0] for pkg in packages)
                if 'debug' in packages_names:
                    fo.write('#!python_d\n')
                fo.write(pyscript)
            link_package(join(src_dir, 'cli-%d.exe' % context.bits), path + '.exe')
        else:
            with open(path, 'w') as fo:
                fo.write('#!%s\n' % python_path)
                fo.write(pyscript)
            os.chmod(path, int('755', 8))


class NoArch(object):

    def link(self, prefix, src_dir):
        pass

    def unlink(self):
        pass


class NoArchPython(NoArch):

    def link(self, prefix, src_dir):
        with open(join(src_dir, "info/files")) as f:
            files = f.read()
        files = files.split("\n")[:-1]

        site_package_files = []
        bin_files = []
        for f in files:
            if f.find("site-packages") > 0:
                site_package_files.append(f[f.find("site-packages"):])
            else:
                if f.find("bin") == 0:
                    bin_files.append(f.replace("bin/", ""))

        site_packages_dir = get_site_packages_dir(prefix)
        bin_dir = get_bin_dir(prefix)
        linked_files = link_files(prefix, '', site_packages_dir, site_package_files, src_dir)
        link_files(prefix, 'python-scripts', bin_dir, bin_files, src_dir)
        compile_missing_pyc(
            prefix, linked_files, join(prefix, site_packages_dir, 'site-packages')
        )

        create_entry_points(src_dir, bin_dir, prefix)

    def unlink(self):
        pass


NOARCH_CLASSES = {
    'python': NoArchPython,
    True: NoArch,
}
