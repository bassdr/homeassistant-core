#!/bin/python

import ast
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import requests
import shutil
import subprocess
import sys
import threading
import traceback

from homeassistant.const import __version__ as homeassistant_version
from .gen_requirements_all import gather_modules, core_requirements, EXCLUDED_REQUIREMENTS_ALL

overlay_dir = Path("/var/db/repos/gentoo-homeassistant")
gentoo_overlay = Path("/var/db/repos/gentoo")

# TODO: improve thread pool logic
# max_workers_lock = threading.Lock()
max_workers = min(32, (os.cpu_count() or 1) + 4)

fetch_pypi_metadata_lock = threading.Lock()
fetch_pypi_metadata_cache = dict[str, dict[str, any]]()


def fetch_pypi_metadata(package_name: str, package_version: str = "") -> dict[str, any]:
    package_name = package_name.strip()
    package_version = package_version.strip()
    url = f'https://pypi.org/pypi/{package_name}{"/" + package_version if package_version else ""}/json'

    with fetch_pypi_metadata_lock:
        if url not in fetch_pypi_metadata_cache:
            try:
                with requests.get(url) as response:
                    response.raise_for_status()
                    fetch_pypi_metadata_cache[url] = response.json() if response.status_code == 200 else {}
            except requests.RequestException as e:
                print(f"Error fetching {url}: {e}", file=sys.stderr)
                return {}
        return fetch_pypi_metadata_cache[url]


def fetch_pypi_index(package_name: str) -> dict[str, any]:
    package_name = package_name.strip()
    url = f'https://pypi.org/simple/{package_name}/'
    headers = {"Accept": "application/vnd.pypi.simple.v1+json"}

    with fetch_pypi_metadata_lock:
        if url not in fetch_pypi_metadata_cache:
            try:
                with requests.get(url, headers=headers) as response:
                    response.raise_for_status()
                    fetch_pypi_metadata_cache[url] = response.json() if response.status_code == 200 else {}
            except requests.RequestException as e:
                print(f"Error fetching {url}: {e}", file=sys.stderr)
                return {}
        return fetch_pypi_metadata_cache[url]


def fetch_pypi_versions(package_name: str) -> set[str]:
    return set(fetch_pypi_index(package_name).get("versions", []))


def fetch_pypi_latest_version(package_name: str) -> str:
    return fetch_pypi_metadata(package_name).get("info", {}).get("version", "")


def fetch_pypi_requires_dist(package_name: str, package_version: str = "") -> set[str]:
    requires_dist = fetch_pypi_metadata(package_name, package_version).get("info", {}).get("requires_dist", [])
    return set(requires_dist) if requires_dist else set()


def fetch_pypi_license(package_name: str, package_version: str = "") -> str:
    return fetch_pypi_metadata(package_name, package_version).get("info", {}).get("license", "")


def fetch_pypi_sdist_info(package_name: str, package_version: str = "") -> tuple[str, str]:
    urls = fetch_pypi_metadata(package_name, package_version).get("urls", [])

    sdist_filenames = {
        file_info.get("filename", file_info.get("url", "").rpartition('/')[2])
        for file_info in urls
        if file_info.get("packagetype") == "sdist"
    }

    for ext in [".tar.gz", ".zip"]:
        for filename in sdist_filenames:
            if filename.endswith(ext):
                return filename.removesuffix(ext), ext

    return "", ""


def manifest_ebuild(ebuild_path: Path) -> None:
    args = ["sudo", "ebuild", str(ebuild_path.absolute()), "manifest"]
    print(" ".join(args))
    result = subprocess.run(args, close_fds=True)
    if result.returncode != 0:
        print(f": failed with return_code={result.returncode}", file=sys.stderr)


treated_packages_lock = threading.Lock()
treated_packages = set[str]()

pip_show_split = re.compile(br'([^: ]+): ?(.*)')
get_revision = re.compile(r'-r(\d+)\.ebuild$')
trailing_numbers = re.compile(r'[-_](\d+)$')

pypi_package_alias = dict[str, str]()
# The fork is actually named dev-python/certifi in gentoo. Versions are not matching and the fork is not providing many
pypi_package_alias["certifi-system-store"] = "certifi"
# This is just an alias but many packages tend to use the short name, complicating the dependencies for nothing
pypi_package_alias["bs4"] = "beautifulsoup4"
# HA is actually still on the old discogs-client, but gentoo is ahead... TODO: downgrade to what HA wants
# pypi_package_alias["discogs-client"] = "python3-discogs-client"
# No more dependency on the old lark-parser. This one is fine.
pypi_package_alias["lark-parser"] = "lark"
# To avoid clashes, better use HA's fork everywhere. Versions are matching
pypi_package_alias["atomicwrites"] = "atomicwrites-homeassistant"
# package is named torch on pypi but pytorch in gentoo
pypi_package_alias["pytorch"] = "torch"

pypi_package_gentoo_name_alias = dict[str, str]()
# package is named torch on pypi but pytorch in gentoo
pypi_package_gentoo_name_alias["torch"] = "pytorch"

pypi_package_version_alias = dict[str, str]()
pypi_package_version_alias["certifi"] = "3024.7.22"  # Versions are not matching and the fork is not providing many
pypi_package_version_alias["rfc3161-client"] = "0.1.2"  # TODO: improve version chooser instead of forcing minor
pypi_package_version_alias["bcrypt-4.2.0"] = "4.2.1"  # TODO: update crates automatically instead
pypi_package_version_alias["uv-0.5.4"] = "0.5.6"  # TODO: update crates automatically instead
pypi_package_version_alias["twistedchecker-0.7"] = "0.7.4"  # TODO: improve version chooser instead of forcing minor
pypi_package_version_alias["array-record-0.6.0"] = "0.5.0"  # 0.6.0 has no tag or sdist...
pypi_package_version_alias["autogen-agentchat-0.2"] = "0.2.40"  # TODO: improve version chooser instead of forcing minor

pypi_test_extras = {"test", "tests", "testing", "dev"}
pypi_build_extras = { "build" }
pypi_default_extras = { "default" }

ebuild_category_override = dict[str, str]()
ebuild_category_override["geopy"] = "sci-geosciences"
ebuild_category_override["tokenizers"] = "sci-libs"
ebuild_category_override["acme"] = "app-crypt"
ebuild_category_override["mutagen"] = "media-libs"
ebuild_category_override["pre-commit"] = "dev-vcs"
ebuild_category_override["python-gitlab"] = "dev-vcs"
ebuild_category_override["scapy"] = "net-analyzer"
ebuild_category_override["shodan"] = "net-analyzer"
ebuild_category_override["speedtest-cli"] = "net-analyzer"
ebuild_category_override["yt-dlp"] = "net-misc"
ebuild_category_override["meson"] = "dev-build"
ebuild_category_override["brotli"] = "app-arch"
ebuild_category_override["cmake"] = "dev-build"
ebuild_category_override["ninja"] = "dev-build"
ebuild_category_override["torch"] = "sci-libs"

ebuild_no_use_python = set[str]()
ebuild_no_use_python.add("uv")

ebuild_extra_use_flags = dict[str, list[str]]()
ebuild_extra_use_flags["brotli"] = ["python"]

gentoo_licenses = [p.name for p in gentoo_overlay.joinpath("licenses").iterdir() if p.is_file()]


def count_quotes(line: str) -> int:
    count = 0
    skip = False
    for char in line:
        if skip:
            skip = False
        elif char == '\\':
            skip = True
        elif char == '"':
            count += 1
    return count


replace_alpha = re.compile(r'(\d+)[._-]?(?:alpha|a)[._-]?(\d+)?')
replace_beta = re.compile(r'(\d+)[._-]?(?:beta|b)[._-]?(\d+)?')
replace_rc = re.compile(r'(\d+)[._-]?rc[._-]?(\d+)?')
replace_post = re.compile(r'(\d+)(?:[._-]?post[._-]?(\d+)?|[_-](\d+))')
replace_dev = re.compile(r'(\d+)[._-]?dev[._-]?(\d+)?')
replace_stars = re.compile(r'[*.]*\*')
remove_v = re.compile(r'v(\d+)')


def normalize_version(ver: str) -> str:
    ver = ver.strip()
    ver = replace_alpha.sub(r"\1_alpha\2", ver)
    ver = replace_beta.sub(r"\1_beta\2", ver)
    ver = replace_rc.sub(r"\1_rc\2", ver)
    ver = replace_post.sub(r"\1_p\2\3", ver)
    ver = replace_dev.sub(r"\1_pre\2", ver)
    ver = replace_stars.sub(r"*", ver)
    ver = remove_v.sub(r"\1", ver)
    return ver.rstrip(r'.-')


tokenizer = re.compile(r'\s*(?P<name>[^\[\]~<>=!()\s,]+)'  # Package or special keyword like extra or python_version
                       r'\s*(?:\[(?P<extras>[^\[\]~<>=!()]+)])?'  # Extras required for this dependency
                       r'\s*\(?\s*(?P<versions>(?:[~<>=!]=?\s*[^\[\]~<>=!()]+\s*)+)?\s*\)?\s*')  # Version requirements
tokenizer_eq = re.compile(r'==\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_cm = re.compile(r'~=\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_gt = re.compile(r'>\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_ge = re.compile(r'>=\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_lt = re.compile(r'<\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_le = re.compile(r'<=\s*["\']?([^~<>=!,"\'\s]+)["\']?')
tokenizer_ne = re.compile(r'!=\s*["\']?([^~<>=!,"\'\s]+)["\']?')
symbol_priority = {'!': -1, '<': -2, '>': -3, '~': -4, '=': -5}  # Special symbols come first, before any ascii
pypi_normalizer = re.compile(r"[._-]+")
dirty_num_split = re.compile(r'\D+')


# noinspection PyTypeChecker
class RequiresDistConditionVisitor(ast.NodeVisitor):
    def __init__(self, depends: str, extras: set[str]):
        self.depends: str = depends
        self.extras: set[str] = extras
        self._or_scope: bool = False
        self._not_scope: bool = False

    def visit_Compare(self, node: ast.Compare) -> any:
        lhs = ast.unparse(node.left).strip('"\'')
        rhs = [ast.unparse(comparator).strip('"\'').strip() for comparator in node.comparators]
        if len(node.ops) != 1 or len(rhs) != 1:
            breakpoint()

        op = node.ops[0]
        rhs = rhs[0]

        expected_map = [
            ("os_name", "posix"),
            ("sys_platform", "linux"),
            ("platform_python_implementation", "CPython"),
            ("platform_system", "Linux"),
            ("implementation_name", "cpython")
        ]

        match op:
            case ast.Eq():
                is_eq = not self._not_scope
                is_neq = self._not_scope
            case ast.NotEq():
                is_eq = self._not_scope
                is_neq = not self._not_scope
            case _:
                is_eq = is_neq = False

        match op:
            case ast.Gt():
                is_gt = not self._not_scope
                is_lte = self._not_scope
            case ast.LtE():
                is_gt = self._not_scope
                is_lte = not self._not_scope
            case _:
                is_gt = is_lte = False

        match op:
            case ast.GtE():
                is_gte = not self._not_scope
                is_lt = self._not_scope
            case ast.Lt():
                is_gte = self._not_scope
                is_lt = not self._not_scope
            case _:
                is_gte = is_lt = False

        match op:
            case ast.In():
                is_in = not self._not_scope
                is_nin = self._not_scope
            case ast.NotIn():
                is_in = self._not_scope
                is_nin = not self._not_scope
            case _:
                is_in = is_nin = False

        for expected_lhs, expected_rhs in expected_map:
            if self.depends:
                if lhs == expected_lhs:
                    expected_rhs = expected_rhs.casefold()
                    if is_eq:
                        if rhs.casefold() != expected_rhs:
                            self.depends = ""
                            break
                    elif is_neq:
                        if rhs.casefold() == expected_rhs:
                            self.depends = ""
                            break
                    else:
                        breakpoint()

        supported_platforms = {"x86_64", "aarch64"}
        if self.depends and lhs == "platform_machine":
            if is_eq:
                if rhs not in supported_platforms:
                    self.depends = ""
            elif is_neq:
                if rhs in supported_platforms:
                    self.depends = ""
            else:
                breakpoint()

        if self.depends and (lhs == "python_version" or lhs == "python_full_version"):
            if is_eq:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version == [3, 12]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
                elif version == [3, 13]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
                else:
                    self.depends = ""
            elif is_lte:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version < [3, 12]:
                    self.depends = ""
                elif version <= [3, 12]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
            elif is_lt:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version <= [3, 12]:
                    self.depends = ""
                elif version <= [3, 13]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
            elif is_gte:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version > [3, 13]:
                    self.depends = ""
                elif version >= [3, 13]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
            elif is_gt:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version >= [3, 13]:
                    self.depends = ""
                elif version >= [3, 12]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
            elif is_neq:
                version = [int(v) if v else 0 for v in dirty_num_split.split(rhs)[0:2]]
                if version == [3, 12]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
                elif version == [3, 13]:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
            elif is_in:
                versions = [[int(v) if v else 0 for v in dirty_num_split.split(v)[0:2]] for v in rhs.split()]
                if [3, 12] in versions and [3, 13] not in versions:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
                elif [3, 12] not in versions and [3, 13] in versions:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
                elif [3, 12] not in versions and [3, 13] not in versions:
                    self.depends = ""
            elif is_nin:
                versions = [[int(v) if v else 0 for v in dirty_num_split.split(v)[0:2]] for v in rhs.split()]
                if [3, 12] in versions and [3, 13] not in versions:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_13{{,t}})"
                elif [3, 12] not in versions and [3, 13] in versions:
                    self.depends = f"$(python_gen_cond_dep '{self.depends}' python3_12)"
                elif [3, 12] in versions and [3, 13] in versions:
                    self.depends = ""
            else:
                breakpoint()

        if self.depends and lhs == "extra":
            self.extras.add(rhs)
            if rhs not in pypi_test_extras and rhs not in pypi_build_extras:
                if is_eq:
                    self.depends = f"{rhs}? ( {self.depends} )"
                elif is_neq:
                    self.depends = f"!{rhs}? ( {self.depends} )"
                else:
                    breakpoint()
            elif not is_eq:
                breakpoint()  # This is a build/test extra, only == is supported

    def visit_BoolOp(self, node: ast.BoolOp):
        match node.op:
            case ast.And():
                is_and = not self._not_scope
                is_or = self._not_scope
            case ast.Or():
                is_and = self._not_scope
                is_or = not self._not_scope
            case _:
                is_and = is_or = False

        old_or_scope = self._or_scope
        if is_and:
            self._or_scope = False
            for compare in node.values:
                self.visit(compare)
        elif is_or:
            self._or_scope = True
            results = set[str]()
            initial_depends = self.depends
            for compare in node.values:
                self.visit(compare)
                if self.depends:
                    results.add(self.depends)
                self.depends = initial_depends

            self.depends = " ".join(results)
        else:
            breakpoint()

        self._or_scope = old_or_scope

    def visit_UnaryOp(self, node: ast.UnaryOp):
        old_not_scope = self._not_scope
        old_or_scope = self._or_scope
        if isinstance(node.op, ast.Not):
            self._not_scope = not self._not_scope
            self._or_scope = not self._or_scope
            self.visit(node.operand)
        else:
            breakpoint()

        self._not_scope = old_not_scope
        self._or_scope = old_or_scope


def gen_python_ebuild(pypi_requires: str) -> tuple[str, str, set[str]]:
    pypi_requires_split = pypi_requires.split(';')
    pypi_requires_tokenized = tokenizer.match(pypi_requires_split[0])
    # TODO: raise an exception here instead, and fix this. Should not happen.
    if pypi_requires_tokenized is None:
        return "", "", set[str]()
    pypi_package = pypi_requires_tokenized.group("name").lower().replace(".", "-").replace("_", "-")
    if pypi_package in pypi_package_alias:
        pypi_package = pypi_package_alias[pypi_package]
    pypi_package_use = pypi_requires_tokenized.group("extras")

    use = list[str]()
    if pypi_package_use is not None:
        use += [pypi_package_use]

    if pypi_package in ebuild_extra_use_flags:
        use += ebuild_extra_use_flags[pypi_package]

    if pypi_package not in ebuild_no_use_python:
        use += [r"${PYTHON_USEDEP}"]

    if len(use) > 0:
        use = "[" + ",".join(use) + "]"
    else:
        use = ''

    gentoo_package_short_name = trailing_numbers.sub(r'_\1', pypi_package)
    if pypi_package in pypi_package_gentoo_name_alias:
        gentoo_package_short_name = pypi_package_gentoo_name_alias[pypi_package]

    if pypi_package in ebuild_category_override:
        gentoo_package_name = ebuild_category_override[pypi_package] + "/" + gentoo_package_short_name
    else:
        gentoo_package_name = "dev-python/" + gentoo_package_short_name

    def full_gentoo_depend(version: str = "", any_version_alias:bool = False) -> str:
        version = version.strip()

        if version and pypi_package + '-' + version in pypi_package_version_alias:
            version = pypi_package_version_alias[pypi_package + '-' + version]
        elif any_version_alias and pypi_package in pypi_package_version_alias:
            version = pypi_package_version_alias[pypi_package]

        if version:
            return gentoo_package_name + '-' + normalize_version(version) + use
        else:
            return gentoo_package_name + use

    gentoo_package_depends = list[str]()
    pypi_package_version = ""
    pypi_versions = pypi_requires_tokenized.group("versions")
    if pypi_versions is not None:
        for ver in tokenizer_eq.findall(pypi_versions):
            exact_dependency = '*' not in ver
            operator = '~' if exact_dependency else '='
            gentoo_package_depends += [operator + full_gentoo_depend(ver, any_version_alias=exact_dependency)]
            if exact_dependency:
                pypi_package_version = ver
        for ver in tokenizer_cm.findall(pypi_versions):
            gentoo_package_depends += [">=" + full_gentoo_depend(ver)]
            gentoo_package_depends += ["=" + full_gentoo_depend(ver.rpartition('.')[0] + '*')]
        for ver in tokenizer_gt.findall(pypi_versions):
            gentoo_package_depends += [">" + full_gentoo_depend(ver)]
        for ver in tokenizer_ge.findall(pypi_versions):
            gentoo_package_depends += [">=" + full_gentoo_depend(ver)]
        for ver in tokenizer_lt.findall(pypi_versions):
            gentoo_package_depends += ["<" + full_gentoo_depend(ver)]
        for ver in tokenizer_le.findall(pypi_versions):
            gentoo_package_depends += ["<=" + full_gentoo_depend(ver)]
        for ver in tokenizer_ne.findall(pypi_versions):
            exact_dependency = '*' not in ver
            operator = '!~' if exact_dependency else '!='
            gentoo_package_depends += [operator + full_gentoo_depend(ver)]

    if len(gentoo_package_depends) > 0:
        gentoo_package_depends = " ".join(gentoo_package_depends)
    else:
        gentoo_package_depends = full_gentoo_depend()

    gentoo_package_extras = set[str]()

    visitor = RequiresDistConditionVisitor(gentoo_package_depends, gentoo_package_extras)
    for condition in pypi_requires_split[1:]:
        if gentoo_package_depends:
            try:
                pypi_condition_tree = ast.parse(condition.strip())
                visitor.visit(pypi_condition_tree)
                gentoo_package_depends = visitor.depends
            except SyntaxError as e:
                print(f"AST parse error in condition {condition}: {e}")

    # tuple that will be returned, contains information for the parent ebuild that depends on this ebuild.
    gentoo_package: tuple[str, str, set[str]] = (
        gentoo_package_name,
        gentoo_package_depends,
        gentoo_package_extras
    )

    # Dependency does not apply. Skip.
    if not gentoo_package_depends:
        return gentoo_package

    # Detect already treated packages to avoid re-generating them multiple times, and avoid multiple thread issues.
    # TODO: take into account the generated versions, as it might happen that a condition requires an older version
    global treated_packages_lock
    global treated_packages
    with treated_packages_lock:
        treated_packages_len = len(treated_packages)
        treated_packages.add(gentoo_package_name)
        if treated_packages_len == len(treated_packages):
            return gentoo_package

    output = defaultdict[str, str](str)

    args = [sys.executable, "-m", "pip", "show", "--verbose", pypi_package]
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=sys.stderr) as proc:
            raw_output, _ = proc.communicate()
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + f": failed with return code: {e.returncode}", file=sys.stderr)
        raw_output = None

    if raw_output is not None:
        last_key = ""
        for output_line in raw_output.split(b'\n'):
            output_pair = pip_show_split.fullmatch(output_line)
            if output_pair is None:
                output[last_key] += '\n' + output_line.decode(sys.stdout.encoding)
            else:
                last_key = output_pair.group(1).decode(sys.stdout.encoding)
                if last_key in output:
                    output[last_key] += '\n'
                output[last_key] += output_pair.group(2).decode(sys.stdout.encoding)

    if not pypi_package_version:
        pypi_package_version = output["Version"].strip()

    pypi_package_version = (
        pypi_package_version_alias.get(pypi_package + '-' + pypi_package_version)
        or pypi_package_version_alias.get(pypi_package)
        or pypi_package_version
    )

    if not pypi_package_version:
        pypi_package_version = fetch_pypi_latest_version(pypi_package)

    # I'm giving up, I can't find a version to generate. The ebuild will have to be created manually to resolve the deps
    if not pypi_package_version:
        return gentoo_package

    # TODO: at least validate the version is valid for this condition
    gentoo_package_version = normalize_version(pypi_package_version)

    version_revision = 0

    for revision in overlay_dir.joinpath(gentoo_package_name).glob(
            gentoo_package_short_name + "-" + gentoo_package_version + "*.ebuild"):
        this_revision = get_revision.search(revision.name)
        if this_revision is None:
            this_revision = 0
        else:
            this_revision = int(this_revision.group(1))
        version_revision = max(this_revision, version_revision)

    for revision in gentoo_overlay.joinpath(gentoo_package_name).glob(
            gentoo_package_short_name + "-" + gentoo_package_version + "*.ebuild"):
        this_revision = get_revision.search(revision.name)
        if this_revision is None:
            this_revision = 0
        else:
            this_revision = int(this_revision.group(1))
        version_revision = max(this_revision + 1, version_revision)

    if version_revision == 0:
        version_revision = ""
    else:
        version_revision = "-r" + str(version_revision)

    has_license = output["License"] in gentoo_licenses
    if not has_license:
        output["License"] = fetch_pypi_license(pypi_package, pypi_package_version)
        has_license = output["License"] in gentoo_licenses

    python_ebuild_dir = overlay_dir.joinpath(gentoo_package_name)
    python_ebuild_dir.mkdir(parents=True, exist_ok=True)
    ebuild_path = python_ebuild_dir.joinpath(
        f'{gentoo_package_short_name}-{gentoo_package_version}{version_revision}.ebuild')
    skel_path = None

    if ebuild_path.exists():
        skel_path = ebuild_path

    if skel_path is None:
        for keywords in ["amd64 arm64", "~amd64 ~arm64"]:
            args = ["equery", "w", gentoo_package_name]
            try:
                equery_output = subprocess.check_output(
                    args, env=dict(os.environ, ACCEPT_KEYWORDS=keywords)
                ).decode(sys.stdout.encoding).strip()
                if not equery_output:
                    continue
                skel_path = Path(equery_output)
                break
            except subprocess.CalledProcessError:
                continue
            except OSError:
                continue

    if skel_path is None or not skel_path.exists():
        # This is a bare minimum fallback for when we can't find a better ebuild.
        # Chances are you'll have to tweak it
        skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    if skel_path.parent.joinpath("files").exists() and not python_ebuild_dir.joinpath("files").exists():
        shutil.copytree(skel_path.parent.joinpath("files"), python_ebuild_dir.joinpath("files"), dirs_exist_ok=True)

    requires_dist = fetch_pypi_requires_dist(pypi_package, pypi_package_version)
    # Only consider the return of pip if the pypi api returned nothing.
    if requires_dist is None or not requires_dist:
        requires_dist = set[str]()
        for require in output["Requires"].split(","):
            require = require.strip()
            if require:
                requires_dist.add(require)

    # Recursively call this function for all requirements
    requirements = list[tuple[str, str, set[str]]]()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(gen_python_ebuild, req): req for req in requires_dist if req is not None}
        for future in as_completed(futures):
            try:
                requirements.append(future.result())
            except Exception as e:
                print(f"Error processing {futures[future]}: {e}", file=sys.stderr)
                traceback.print_exc()

    has_requirements = False
    has_build_req = False
    has_extras = False
    has_test_extra = False
    has_build_extra = False
    extras = set[str]()
    for requirement, depends, extra in requirements:
        if not depends.strip():
            continue
        if not extra:
            has_requirements = True
            continue
        if pypi_test_extras.intersection(extra):
            has_build_req = True
            has_test_extra = True
        elif pypi_build_extras.intersection(extra):
            has_build_req = True
            has_build_extra = True
        else:
            has_requirements = True
            extras.update(extra)
            has_extras = True

    if has_test_extra:
        extras.add("test")

    pypi_p, pypi_sdist_ext = fetch_pypi_sdist_info(pypi_package, pypi_package_version)
    if pypi_sdist_ext and pypi_sdist_ext != ".tar.gz":
        has_build_req = True
    pypi_pn, _, pypi_pv = pypi_p.rpartition('-')
    pypi_normalize = False
    if pypi_pn == pypi_normalizer.sub("_", gentoo_package_short_name).lower():
        pypi_normalize = True
        pypi_pn = "${PN}"
    elif pypi_pn == gentoo_package_short_name:
        pypi_normalize = False
        pypi_pn = "${PN}"
    elif pypi_pn == pypi_normalizer.sub("_", pypi_package).lower():
        pypi_normalize = True

    if pypi_pv == gentoo_package_version:
        pypi_pv = "${PV}"

    update_ebuild = ebuild_path.parent == skel_path.parent
    if update_ebuild:
        skel_path = skel_path.rename(skel_path.parent.joinpath(skel_path.name + ".tmp"))

    # TODO: should be an option, as we might want to keep old ebuilds sometimes
    for old_ebuild in ebuild_path.parent.glob('*.ebuild'):
        old_ebuild.unlink()

    with ebuild_path.open("w") as ebuild, skel_path.open("r") as old_ebuild:
        print("Creating " + ebuild.name)

        done = defaultdict[str, bool](bool)
        skip_empty_lines = False
        skip_commented_lines = False
        skip_multiline_quotes = False

        # DISTUTILS_SINGLE_IMPL ebuild detected. GENERATED_{R,B}DEPEND will call python_gen_cond_dep.
        # TODO: Packages that depends on a single_impl ebuild won't work right now.
        single_impl = False

        # By default, assumes we are using the pypi.eclass if we could fetch anything from pypi json api.
        # If the ebuild does not contain the "inherit pypi" line, this will be turned off later.
        # When inherit_pypi is off, SRC_URI and S are left untouched, and app-arch/unzip will never be added to BDEPEND
        inherit_pypi = pypi_p != ""

        python_compat_ebuild = pypi_package not in ebuild_no_use_python

        def append_generated(variable: str, generated_variable: str, input_line: str) -> None:
            nonlocal done, ebuild, skip_empty_lines
            # Old GENERATED variable, we may delete later.
            input_line = input_line.replace("${GENERATED_DEPEND} ", "")
            input_line = input_line.replace("${GENERATED_DEPEND}", "")
            generated_variable = "${" + generated_variable + "}"
            if f'{variable}=""' in input_line:
                ebuild.write(input_line.replace(f'{variable}=""', f'{variable}="{generated_variable}"').rstrip() + '\n')
            elif generated_variable not in input_line:
                ebuild.write(input_line.replace(f'{variable}="', f'{variable}="{generated_variable} ').rstrip() + '\n')
                skip_empty_lines = True
            else:
                ebuild.write(input_line.rstrip() + '\n')
            done[variable] = True

        for line in old_ebuild:
            # Only modify comments if they were generated by this script. Everything else is kept as is.
            if (stripped_line := line.strip()) == "" or stripped_line.startswith('#'):
                if "could not be inserted in this ebuild" in stripped_line:
                    skip_empty_lines = True
                    continue
                if "# Content: " in stripped_line:
                    skip_empty_lines = True
                    skip_commented_lines = True

                if skip_empty_lines and stripped_line == "":
                    continue

                if skip_commented_lines:
                    continue

                ebuild.write(line.rstrip() + '\n')
                continue

            skip_empty_lines = False
            skip_commented_lines = False

            odd_quote = count_quotes(line) % 2 == 1
            if skip_multiline_quotes:
                skip_multiline_quotes = not odd_quote
            elif "DISTUTILS_SINGLE_IMPL=" in line:
                if not line.rstrip().endswith('='):
                    single_impl = True
                    ebuild.write("DISTUTILS_SINGLE_IMPL=1\n")
            elif (has_requirements and not done["Requires"] or has_extras and (not done["extras"] or not done["IUSE"]))\
                    and "RDEPEND=" in line:
                if has_extras and not done["extras"]:
                    ebuild.write('GENERATED_IUSE="')
                    ebuild.write(" ".join([f"+{extra}" if extra in pypi_default_extras else extra
                                           for extra in sorted(extras)]))
                    ebuild.write('"\n')
                    done["extras"] = True
                    done["IUSE"] = False
                if has_extras and not done["IUSE"]:
                    ebuild.write('IUSE="${GENERATED_IUSE}"\n')
                    done["IUSE"] = True
                generated_rdepend = "GENERATED_RDEPEND=" in line
                skip_multiline_quotes = odd_quote and generated_rdepend
                # For debugging if everything is there, maybe can be removed when confident
                if len(requires_dist) > 0 and not done["REQUIRES_DIST"]:
                    ebuild.write('REQUIRES_DIST="\n\t{}\n"\n'.format('\n\t'.join(sorted(requires_dist))
                                                                     .replace('"', "'")))
                    done["REQUIRES_DIST"] = True
                ebuild.write('GENERATED_RDEPEND="${RDEPEND}')
                if single_impl:
                    ebuild.write(" $(python_gen_cond_dep '")
                ebuild.write('\n')
                done["RDEPEND"] = False
                already_added = set[str]()
                for requirement, depends, extra in \
                        sorted(requirements, key=lambda x: (x[0], [symbol_priority.get(c, ord(c)) for c in x[1]])):
                    if not depends:
                        continue
                    if depends in already_added:
                        continue
                    if extra and (pypi_test_extras.intersection(extra) or pypi_build_extras.intersection(extra)):
                        continue
                    already_added.add(depends)
                    ebuild.write(f"\t{depends}\n")
                if single_impl:
                    ebuild.write("')")
                ebuild.write('"\n')
                done["Requires"] = True
                if not generated_rdepend:
                    append_generated("RDEPEND", "GENERATED_RDEPEND", line)
            elif has_requirements and not done["RDEPEND"] and 'RDEPEND="' in line and done["Requires"]:
                append_generated("RDEPEND", "GENERATED_RDEPEND", line)
            elif 'GENERATED_RDEPEND="' in line:
                skip_multiline_quotes = odd_quote
            elif has_extras and not done["extras"] and "IUSE=" in line:
                generated_iuse = "GENERATED_IUSE=" in line
                skip_multiline_quotes = odd_quote and generated_iuse
                ebuild.write(f'GENERATED_IUSE="{" ".join(sorted(extras))}"\n')
                done["extras"] = True
                if not generated_iuse:
                    append_generated("IUSE", "GENERATED_IUSE", line)
            elif has_extras and 'IUSE="' in line and done["extras"]:
                skip_multiline_quotes = odd_quote
                if not done["IUSE"]:
                    append_generated("IUSE", "GENERATED_IUSE", line)
            elif not has_extras and 'GENERATED_IUSE=' in line:
                skip_multiline_quotes = odd_quote
            elif not has_extras and 'IUSE=' in line and "${GENERATED_IUSE}" in line:
                cleaned_line = line.replace("${GENERATED_IUSE} ", "").replace("${GENERATED_IUSE}", "")
                if 'IUSE=""' not in cleaned_line.strip():
                    ebuild.write(cleaned_line.rstrip() + '\n')
            elif has_build_req and (inherit_pypi or has_build_extra or has_test_extra) \
                    and not done["BDEPEND"] \
                    and ("distutils_enable_tests" in line or "GENERATED_BDEPEND=" in line or 'BDEPEND="' in line):
                generated_bdepend = "GENERATED_BDEPEND=" in line
                skip_multiline_quotes = odd_quote and generated_bdepend
                distutils_enable_tests = "distutils_enable_tests" in line
                if distutils_enable_tests:
                    ebuild.write(line.rstrip() + '\n')

                # For debugging if everything is there, maybe can be removed when confident
                if len(requires_dist) > 0 and not done["REQUIRES_DIST"]:
                    ebuild.write('REQUIRES_DIST="\n\t{}\n"\n'.format('\n\t'.join(sorted(requires_dist))
                                                                     .replace('"', "'")))
                    done["REQUIRES_DIST"] = True
                ebuild.write('GENERATED_BDEPEND="${BDEPEND}\n')
                if pypi_sdist_ext != ".tar.gz" and inherit_pypi:
                    ebuild.write('\tapp-arch/unzip\n')
                if has_build_extra or has_test_extra:
                    if single_impl:
                        ebuild.write("\t$(python_gen_cond_dep '\n")
                    already_added = set[str]()
                    if has_build_extra:
                        for requirement, depends, extra in \
                                sorted(requirements,
                                       key=lambda x: (x[0], [symbol_priority.get(c, ord(c)) for c in x[1]])):
                            if not depends:
                                continue
                            if depends in already_added:
                                continue
                            if not extra:
                                continue
                            if not pypi_build_extras.intersection(extra):
                                continue
                            already_added.add(depends)
                            ebuild.write(f"\t{depends}\n")
                    if has_test_extra:
                        ebuild.write('\ttest? (\n')
                        for requirement, depends, extra in \
                                sorted(requirements,
                                       key=lambda x: (x[0], [symbol_priority.get(c, ord(c)) for c in x[1]])):
                            if not depends:
                                continue
                            if depends in already_added:
                                continue
                            if not extra:
                                continue
                            if not pypi_test_extras.intersection(extra):
                                continue
                            already_added.add(depends)
                            ebuild.write(f"\t\t{depends}\n")
                        ebuild.write('\t)\n')
                    if single_impl:
                        ebuild.write("\t')\n")
                ebuild.write('"\n')
                if not generated_bdepend and not distutils_enable_tests:
                    append_generated("BDEPEND", "GENERATED_BDEPEND", line)
                else:
                    ebuild.write('BDEPEND="${GENERATED_BDEPEND}"\n')
                    done["BDEPEND"] = True
            elif 'BDEPEND+=" test? (' in line:
                skip_multiline_quotes = odd_quote
            elif 'GENERATED_BDEPEND="' in line:
                skip_multiline_quotes = odd_quote
            elif (has_build_req or pypi_sdist_ext != ".tar.gz" and inherit_pypi) and 'BDEPEND="' in line \
                    and done["BDEPEND"]:
                cleaned_line = line
                for remove in {"GENERATED_BDEPEND", "BDEPEND"}:
                    cleaned_line = cleaned_line.replace('${' + remove + '} ', '')
                    cleaned_line = cleaned_line.replace('${' + remove + '}', '')
                if cleaned_line.strip() != 'BDEPEND=""':
                    ebuild.write(cleaned_line.replace('BDEPEND="', 'BDEPEND+=" ').rstrip() + '\n')
            elif has_license and not done["License"] and "LICENSE=" in line:
                skip_multiline_quotes = odd_quote
                ebuild.write('LICENSE="' + output["License"] + '"\n')
                done["License"] = True
            elif not done["Summary"] and "DESCRIPTION=" in line:
                skip_multiline_quotes = odd_quote
                ebuild.write('DESCRIPTION="' + output["Summary"].replace(r'"', r'\"').replace(r'`', r'\`') + '"\n')
                done["Summary"] = True
            elif not done["Project-URLs"] and "HOMEPAGE=" in line:
                skip_multiline_quotes = odd_quote
                ebuild.write('HOMEPAGE="\n  https://pypi.org/project/{}/{}"\n'
                             .format(pypi_normalizer.sub('-', pypi_package).lower(),  # TODO: use pypi's name
                                     output["Project-URLs"].replace(r'"', r'\"').replace(r'`', r'\`')))
                done["Project-URLs"] = True
            elif pypi_p and "PYPI_PN=" in line:
                skip_multiline_quotes = odd_quote
                skip_empty_lines = True
            elif pypi_p and 'PYPI_NO_NORMALIZE=' in line:
                skip_multiline_quotes = odd_quote
                skip_empty_lines = True
            elif pypi_p and "SRC_URI=" in line and inherit_pypi:
                skip_multiline_quotes = odd_quote
                skip_empty_lines = True
            elif pypi_p and line.startswith('S=') and inherit_pypi:
                skip_multiline_quotes = odd_quote
                skip_empty_lines = True
            elif pypi_p and (not done["PYPI_PN"] or not done["SRC_URI"]) and "inherit" in line and "pypi" in line:
                inherit_pypi = True
                no_normalize_arg = ""
                if not pypi_normalize:
                    no_normalize_arg = "--no-normalize "
                if not pypi_normalize and pypi_pv == "${PV}" and pypi_sdist_ext != ".zip":
                    ebuild.write('PYPI_NO_NORMALIZE=1\n')
                if pypi_pn != "${PN}" and pypi_pn != "${PYPI_PN}":
                    ebuild.write(f'PYPI_PN="{pypi_pn}"\n')
                    pypi_pn = "${PYPI_PN}"
                done["PYPI_PN"] = True
                ebuild.write(line.rstrip() + '\n')
                if pypi_pn != "${PN}" and pypi_pn != "${PYPI_PN}":
                    pypi_pn = "${PYPI_PN}"
                if pypi_pv != "${PV}" or pypi_sdist_ext == ".zip":
                    ebuild.write(f'SRC_URI="$(pypi_sdist_url {no_normalize_arg}{pypi_pn} {pypi_pv}')
                    if pypi_sdist_ext == ".zip":
                        ebuild.write(" .zip")
                    ebuild.write(')"\n')
                if (pypi_pn != "${PN}" and pypi_pn != "${PYPI_PN}") or pypi_pv != "${PV}":
                    ebuild.write('S="${WORKDIR}/')
                    ebuild.write(f'$(pypi_normalize_name {pypi_pn})' if pypi_normalize else pypi_pn)
                    ebuild.write(f'-{pypi_pv}"\n')
                done["SRC_URI"] = True
                ebuild.write('\n')
                skip_empty_lines = True
            elif pypi_p and (not done["PYPI_PN"] or not done["SRC_URI"]) and "inherit" in line and "pypi" not in line:
                # Assume that if we inherit without pypi, SRC_URI and S are manually set
                inherit_pypi = False
                if not done["PYPI_PN"]:
                    del done["PYPI_PN"]
                if not done["SRC_URI"]:
                    del done["SRC_URI"]
                if not done["BDEPEND"]:
                    del done["BDEPEND"]
                ebuild.write(line.rstrip() + '\n')
            elif not done["KEYWORDS"] and "KEYWORDS=" in line:
                skip_multiline_quotes = odd_quote
                ebuild.write('KEYWORDS="amd64 arm64"\n')
                done["KEYWORDS"] = True
            elif python_compat_ebuild and not done["PYTHON_COMPAT"] and "PYTHON_COMPAT=" in line:
                ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n")
                done["PYTHON_COMPAT"] = True
            elif "REQUIRES_DIST=" in line:
                skip_multiline_quotes = odd_quote
            elif "GENERATED_" in line:
                if "GENERATED_DEPEND=" in line:
                    skip_multiline_quotes = odd_quote
                    skip_empty_lines = True
                else:
                    cleaned_line = line
                    for remove in {"RDEPEND", "BDEPEND", "DEPEND", "IUSE"}:
                        cleaned_line = cleaned_line.replace(f'${{GENERATED_{remove}}} ', '')
                        cleaned_line = cleaned_line.replace(f'${{GENERATED_{remove}}}', '')
                    if (stripped := cleaned_line.strip()) != 'RDEPEND=""' and stripped != 'BDEPEND=""':
                        ebuild.write(cleaned_line.rstrip() + '\n')
            elif 'IUSE=""' in line or 'RDEPEND=""' in line or 'BDEPEND=""' in line:
                pass
            else:
                ebuild.write(line.rstrip() + '\n')

        for key, value in done.items():
            if not value:
                ebuild.write(f"# {key} could not be inserted in this ebuild\n")

    if update_ebuild:
        skel_path.unlink()

    manifest_ebuild(ebuild_path)
    return gentoo_package


def gen_homeassistant_ebuilds() -> None:
    metadata_dir = overlay_dir.joinpath("metadata")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    with metadata_dir.joinpath("layout.conf").open("w") as layout:
        layout.write("masters = gentoo")

    profile_dir = overlay_dir.joinpath("profiles")
    profile_dir.mkdir(parents=True, exist_ok=True)
    with profile_dir.joinpath("repo_name").open("w") as repo_name:
        repo_name.write("gentoo-homeassistant\n")
    with profile_dir.joinpath("categories").open("w") as categories:
        categories.write("homeassistant-base\n")

    deptree = defaultdict[str, set[str]](set[str])
    excluded_modules = set[str]()
    for module_dep, module_names in gather_modules().items():
        excluded = module_dep.rpartition('==')[0] in EXCLUDED_REQUIREMENTS_ALL
        for module_name in module_names:
            if excluded:
                excluded_modules.add(module_name)
            deptree[module_name].add(module_dep)

    # Excluded requirements makes the whole module unusable, lets not generate it at all.
    for excluded_module in excluded_modules:
        del deptree[excluded_module]

    for core_dep in core_requirements():
        deptree["homeassistant.core"].add(core_dep)

    # homeassistant-base/ha-core has a dependency on dev-python/homeassistant.
    # ha-core will have dependencies to specific (frozen) versions while homeassistant will follow pypi's requirements
    deptree["homeassistant.core"].add("homeassistant==" + homeassistant_version)

    def module_task(module: str, deps: set[str]) -> None:
        tokenized_module = trailing_numbers.sub(r'\1', module).split('.')
        if len(tokenized_module) > 1 and tokenized_module[0] == "homeassistant":
            tokenized_module[0] = "ha"
        if len(tokenized_module) > 2 and tokenized_module[1] == "components":
            tokenized_module[1] = "comp"
        gentoo_module = "-".join(tokenized_module).replace(".", "-").replace("_", "-")
        module_deps = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(gen_python_ebuild, dep): dep for dep in deps}
            for future in as_completed(futures):
                try:
                    module_deps += [future.result()]
                except Exception as e:
                    print(f"Error in module_task for {futures[future]}: {e}", file=sys.stderr)
                    traceback.print_exc()
        module_deps.sort(key=lambda dep: [s.casefold() if s else "" for s in tokenizer.match(dep[0]).group(1, 3, 2)])

        ebuild_dir = overlay_dir.joinpath("homeassistant-base").joinpath(gentoo_module)

        global treated_packages_lock
        global treated_packages
        with treated_packages_lock:
            treated_packages.add("homeassistant-base" + '/' + gentoo_module)

        ebuild_dir.mkdir(parents=True, exist_ok=True)
        ebuild_path = ebuild_dir.joinpath(gentoo_module + "-" + homeassistant_version + ".ebuild")

        # TODO: should be an option, as we might want to keep old ebuilds sometimes
        for old_ebuild in ebuild_path.parent.glob('*.ebuild'):
            old_ebuild.unlink()

        with ebuild_path.open("w") as ebuild:
            ebuild.write("EAPI=8\n\n")
            ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n\n")
            ebuild.write("inherit python-r1\n\n")
            ebuild.write(f'DESCRIPTION="Home Assistant Meta-Package {module}"\n')
            ebuild.write('LICENSE="Apache-2.0"\n\n')
            ebuild.write('SLOT="0"\n')
            ebuild.write('KEYWORDS="amd64 arm64"\n\n')
            ebuild.write('RDEPEND="\n')
            ebuild.writelines(f'\t{depends}\n' for _, depends, _ in module_deps if depends)
            ebuild.write('"\n')
        manifest_ebuild(ebuild_path)

    with ThreadPoolExecutor(max_workers=max_workers) as module_executor:
        module_futures = {module_executor.submit(module_task, module, deps): module for module, deps in deptree.items()}
        for module_future in as_completed(module_futures):
            try:
                module_future.result()
            except Exception as module_e:
                print(f"Error in gen_homeassistant_ebuilds for {module_futures[module_future]}: {module_e}",
                      file=sys.stderr)
                traceback.print_exc()

    # TODO: should be an option, as we might want to keep old ebuilds sometimes
    global treated_packages_lock
    global treated_packages
    with treated_packages_lock:
        # protect some special files:
        treated_packages.add('.git/info')
        treated_packages.add('metadata/md5-cache')
        treated_packages.add('acct-group/homeassistant')
        treated_packages.add('acct-user/homeassistant')

        # delete everything else
        for folder in overlay_dir.glob('*/*/'):
            if folder.name.startswith('.'):
                continue
            rel_path = str(folder.relative_to(overlay_dir)).rstrip('/')
            if rel_path.startswith('.'):
                continue
            if rel_path not in treated_packages:
                shutil.rmtree(folder)


if __name__ == "__main__":
    gen_homeassistant_ebuilds()
