#!/bin/python

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import isclose
import os
from pathlib import Path
import re
import requests
import shutil
import subprocess
import sys
import threading
import traceback

from .gen_requirements_all import gather_modules, core_requirements

# TODO: detect this somehow
homeassistant_version = "2024.12.1"
overlay_dir = Path("/var/db/repos/gentoo-homeassistant")
gentoo_overlay = Path("/var/db/repos/gentoo")

fetch_pypi_metadata_lock = threading.Lock()
fetch_pypi_metadata_cache = dict[str, dict[str, dict[str, any]]]()


def fetch_pypi_metadata(package_name: str, package_version: str = "") -> dict[str, dict[str, any]]:
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


def fetch_pypi_versions(package_name: str) -> set[str]:
    return set(fetch_pypi_metadata(package_name).get("releases", {}).keys())


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
get_revision = re.compile(r'-r([0-9]+)\.ebuild$')
trailing_numbers = re.compile(r'[-_]([0-9]+)$')

pypi_package_alias = dict[str, str]()
pypi_package_alias["certifi-system-store"] = "certifi"  # the fork is actually named dev-python/certifi in gentoo

pypi_package_version_alias = dict[str, str]()
pypi_package_version_alias["certifi"] = "3024.7.22"
pypi_package_version_alias["bcrypt-4.2.0"] = "4.2.1"
pypi_package_version_alias["uv-0.5.4"] = "0.5.6"
pypi_package_version_alias["twistedchecker-0.7"] = "0.7.4"
pypi_package_version_alias["array_record-0.6.0"] = "0.5.0"
pypi_package_version_alias["array-record-0.6.0"] = "0.5.0"
pypi_package_version_alias["autogen-agentchat-0.2"] = "0.2.40"
pypi_package_version_alias["autogen_agentchat-0.2"] = "0.2.40"

pypi_test_extras = {"test", "tests", "testing", "dev"}

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

ebuild_no_use_python = set[str]()
ebuild_no_use_python.add("dev-python/uv")

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


remove_v = re.compile(r'v([0-9]+)')
replace_alpha = re.compile(r'(\d+)[.-_]?(?:a|alpha)(\d+)')
replace_beta = re.compile(r'(\d+)[.-_]?(?:b|beta)(\d+)')
replace_rc = re.compile(r'(\d+)[.-_]?rc(\d+)')
replace_post = re.compile(r'(\d+)(?:[.-_]?post|[-_])(\d+)')
replace_dev = re.compile(r'(\d+)[.-_]?dev')


def normalize_version(ver: str) -> str:
    ver = replace_alpha.sub(r"\1_alpha\2", ver)
    ver = ver.replace(r"-alpha", "_alpha")
    ver = ver.replace(r".alpha", "_alpha")
    ver = replace_beta.sub(r"\1_beta\2", ver)
    ver = ver.replace(r"-beta", "_beta")
    ver = ver.replace(r".beta", "_beta")
    ver = replace_rc.sub(r"\1_rc\2", ver)
    ver = ver.replace(r"-rc", "_rc")
    ver = ver.replace(r".rc", "_rc")
    ver = replace_post.sub(r"\1_p\2", ver)
    ver = ver.replace(r"-post", r"_p")
    ver = ver.replace(r".post", r"_p")
    ver = ver.replace(r"post", r"_p")
    ver = replace_dev.sub(r"\1_pre", ver)
    ver = remove_v.sub(r"\1", ver)
    ver = trailing_numbers.sub(r"_p\1", ver)
    return ver.strip().rstrip(r'.*-')  # maybe the '*' should be left alone, but it is not always a valid atom


tokenizer = re.compile(r'\s*([^\[\]~<>=!()\s,]+)'  # Name of the package or special keyword like extra or python_version
                       r'\s*(?:\[([^\[\]~<>=!()]+)])?'  # Extras required for this dependency
                       r'\s*\(?\s*((?:[~<>=!]=?\s*[^\[\]~<>=!()\s,]+\s*)+)?\s*\)?\s*')  # Version requirements
tokenizer_eq = re.compile(r'[=~]=\s*["\']?([^~<>=!,"\']+)["\']?')
tokenizer_gt = re.compile(r'>\s*["\']?([^~<>=!,"\']+)["\']?')
tokenizer_ge = re.compile(r'>=\s*["\']?([^~<>=!,"\']+)["\']?')
tokenizer_lt = re.compile(r'<\s*["\']?([^~<>=!,"\']+)["\']?')
tokenizer_le = re.compile(r'<=\s*["\']?([^~<>=!,"\']+)["\']?')
tokenizer_ne = re.compile(r'!=\s*["\']?([^~<>=!,"\']+)["\']?')
symbol_priority = {'!': -1, '<': -2, '>': -3, '~': -4, '=': -6}
pypi_normalizer = re.compile(r"[._-]+")


def gen_python_ebuild(pypi_requires: str) -> tuple[str, str, dict[str, set[str]]]:
    pypi_requires_split = pypi_requires.split(';')
    pypi_requires_tokenized = tokenizer.match(pypi_requires_split[0])
    # TODO: raise an exception here instead, and fix this. Should not happen.
    if pypi_requires_tokenized is None:
        return "", "", dict[str, set[str]]()
    pypi_package = pypi_requires_tokenized[1]
    if pypi_package in pypi_package_alias:
        pypi_package = pypi_package_alias[pypi_package]
    pypi_package_use = pypi_requires_tokenized[2]

    use = list[str]()
    if pypi_package_use is not None:
        use += [pypi_package_use]

    if pypi_package not in ebuild_no_use_python:
        use += [r"${PYTHON_USEDEP}"]

    if len(use) > 0:
        use = "[" + ",".join(use) + "]"
    else:
        use = ''

    gentoo_package_short_name = pypi_package.lower().replace(".", "-").replace("_", "-")
    gentoo_package_short_name = trailing_numbers.sub(r'_\1', gentoo_package_short_name)
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
    if pypi_requires_tokenized[3] is not None:
        pypi_package_version_eq = tokenizer_eq.search(pypi_requires_tokenized[3])
        pypi_package_version_gt = tokenizer_gt.search(pypi_requires_tokenized[3])
        pypi_package_version_ge = tokenizer_ge.search(pypi_requires_tokenized[3])
        pypi_package_version_lt = tokenizer_lt.search(pypi_requires_tokenized[3])
        pypi_package_version_le = tokenizer_le.search(pypi_requires_tokenized[3])
        pypi_package_version_ne = tokenizer_ne.search(pypi_requires_tokenized[3])
        if pypi_package_version_eq is not None:
            gentoo_package_depends += ["~" + full_gentoo_depend(pypi_package_version_eq[1], any_version_alias=True)]
            if not pypi_package_version_eq[1].strip().endswith('*'):
                pypi_package_version = pypi_package_version_eq[1].strip()
        if pypi_package_version_gt is not None:
            gentoo_package_depends += [">" + full_gentoo_depend(pypi_package_version_gt[1])]
        if pypi_package_version_ge is not None:
            gentoo_package_depends += [">=" + full_gentoo_depend(pypi_package_version_ge[1])]
        if pypi_package_version_lt is not None:
            gentoo_package_depends += ["<" + full_gentoo_depend(pypi_package_version_lt[1])]
        if pypi_package_version_le is not None:
            gentoo_package_depends += ["<=" + full_gentoo_depend(pypi_package_version_le[1])]
        if pypi_package_version_ne is not None:
            gentoo_package_depends += ["!=" + full_gentoo_depend(pypi_package_version_ne[1])]

    if len(gentoo_package_depends) > 0:
        gentoo_package_depends = " ".join(gentoo_package_depends)
    else:
        gentoo_package_depends = full_gentoo_depend()

    gentoo_package_conditions = defaultdict[str, set[str]](set[str])
    for condition in pypi_requires_split[1:]:
        pypi_condition_tokenized = tokenizer.match(condition)
        if pypi_condition_tokenized is not None and \
                pypi_condition_tokenized[1] is not None and pypi_condition_tokenized[3] is not None:
            pypi_condition_tokenized_eq = tokenizer_eq.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_eq is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add(pypi_condition_tokenized_eq[1])
            pypi_condition_tokenized_gt = tokenizer_gt.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_gt is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add('> ' + pypi_condition_tokenized_gt[1])
            pypi_condition_tokenized_ge = tokenizer_ge.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_ge is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add('>= ' + pypi_condition_tokenized_ge[1])
            pypi_condition_tokenized_lt = tokenizer_lt.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_lt is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add('< ' + pypi_condition_tokenized_lt[1])
            pypi_condition_tokenized_le = tokenizer_le.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_le is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add('<= ' + pypi_condition_tokenized_le[1])
            pypi_condition_tokenized_ne = tokenizer_ne.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_ne is not None:
                gentoo_package_conditions[pypi_condition_tokenized[1]].add('!= ' + pypi_condition_tokenized_ne[1])

    conditions_map = [
        ("os_name", "posix"),
        ("sys_platform", "linux"),
        ("platform_python_implementation", "CPython"),
        ("platform_system", "Linux"),
        ("implementation_name", "cpython")
    ]

    for condition, expected_value in conditions_map:
        expected_value = expected_value.casefold()
        if gentoo_package_depends and any(
                value.casefold() != expected_value for value in gentoo_package_conditions[condition]):
            gentoo_package_depends = ""
            break

    # TODO: platform_machine x86_64 or aarch64

    if gentoo_package_depends:
        for python_version in gentoo_package_conditions["python_version"]:
            python_version = python_version.split()
            python_version_operator = python_version[0] if len(python_version) > 1 else ""
            python_version = python_version[1] if len(python_version) > 1 else python_version[0]
            python_version = float(".".join(python_version.split('.')[0:2]))

            if python_version_operator == "":
                if isclose(python_version, 3.12, abs_tol=0.0001):
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_12)"
                elif isclose(python_version, 3.13, abs_tol=0.0001):
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_13{{,t}})"
                else:
                    gentoo_package_depends = ""
                    break
            elif python_version_operator == "<=":
                if isclose(python_version, 3.12, abs_tol=0.0001) or python_version <= 3.12:
                    gentoo_package_depends = ""
                    break
                elif isclose(python_version, 3.13, abs_tol=0.0001) or python_version <= 3.13:
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_12)"
            elif python_version_operator == "<":
                if not isclose(python_version, 3.12, abs_tol=0.0001) and python_version < 3.12:
                    gentoo_package_depends = ""
                    break
                elif not isclose(python_version, 3.13, abs_tol=0.0001) and python_version < 3.13:
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_12)"
            elif python_version_operator == ">=":
                if isclose(python_version, 3.13, abs_tol=0.0001) or python_version >= 3.13:
                    gentoo_package_depends = ""
                    break
                elif isclose(python_version, 3.12, abs_tol=0.0001) or python_version >= 3.12:
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_13{{,t}})"
            elif python_version_operator == ">":
                if not isclose(python_version, 3.13, abs_tol=0.0001) and python_version > 3.13:
                    gentoo_package_depends = ""
                    break
                elif not isclose(python_version, 3.12, abs_tol=0.0001) and python_version > 3.12:
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_13{{,t}})"
            elif python_version_operator == "!=":
                if isclose(python_version, 3.12, abs_tol=0.0001):
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_13{{,t}})"
                elif isclose(python_version, 3.13, abs_tol=0.0001):
                    gentoo_package_depends = f"$(python_gen_cond_dep '{gentoo_package_depends}' python3_12)"

    if gentoo_package_depends:
        for extra in gentoo_package_conditions["extra"]:
            if extra not in pypi_test_extras:
                gentoo_package_depends = f"{extra}? ( {gentoo_package_depends} )"

    # tuple that will be returned, contains information for the parent ebuild that depends on this ebuild.
    gentoo_package: tuple[str, str, dict[str, set[str]]] = (
        gentoo_package_name,
        gentoo_package_depends,
        gentoo_package_conditions
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
        print(" ".join(args) + f": failed returncode={e.returncode}", file=sys.stderr)
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
    if requires_dist is None:
        requires_dist = set[str]()

    for require in output["Requires"].split(","):
        require = require.strip()
        if require:
            requires_dist.add(require)

    # Recursively call this function for all requirements
    requirements = list[tuple[str, str, dict[str, set[str]]]]()
    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(gen_python_ebuild, req): req for req in requires_dist if req is not None}
        for future in as_completed(futures):
            try:
                requirements.append(future.result())
            except Exception as e:
                print(f"Error processing {futures[future]}: {e}", file=sys.stderr)
                traceback.print_exc()

    has_requirements = False
    has_extras = False
    has_tests = False
    extras = set[str]()
    for requirement, depends, conditions in requirements:
        if not depends:
            continue
        has_requirements = True
        extra = conditions.get("extra", set[str]())
        if not extra:
            continue
        if pypi_test_extras.intersection(extra):
            has_tests = True
        else:
            extras.update(extra)
            has_extras = True

    pypi_p, pypi_sdist_ext = fetch_pypi_sdist_info(pypi_package, pypi_package_version)
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
        skip_commented_or_empty_lines = False
        multiline = False
        single_impl = False
        import_pypi = True
        if not pypi_p:
            import_pypi = False

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
            if skip_empty_lines:
                if line.strip() == "":
                    continue
                skip_empty_lines = False
            if skip_commented_or_empty_lines:
                if (stripped_line := line.strip()) == "" or stripped_line.startswith('#'):
                    continue
                skip_commented_or_empty_lines = False

            odd_quote = count_quotes(line) % 2 == 1
            if multiline:
                multiline = not odd_quote
            elif "DISTUTILS_SINGLE_IMPL=" in line:
                if not line.rstrip().endswith('='):
                    single_impl = True
                    ebuild.write("DISTUTILS_SINGLE_IMPL=1\n")
            elif (has_requirements and not done["Requires"] or has_extras and (not done["extras"] or not done["IUSE"]))\
                    and "RDEPEND=" in line:
                if has_extras and not done["extras"]:
                    ebuild.write(f'GENERATED_IUSE="{" ".join(sorted(extras))}"\n')
                    done["extras"] = True
                    done["IUSE"] = False
                if has_extras and not done["IUSE"]:
                    ebuild.write('IUSE="${GENERATED_IUSE}"\n')
                    done["IUSE"] = True
                generated_depend = "GENERATED_RDEPEND=" in line
                multiline = odd_quote and generated_depend
                ebuild.write('GENERATED_RDEPEND="${RDEPEND}')
                if single_impl:
                    ebuild.write(" $(python_gen_cond_dep '")
                ebuild.write('\n')
                done["RDEPEND"] = False
                already_added = set[str]()
                for requirement, depends, conditions in \
                        sorted(requirements, key=lambda x: (x[0], [symbol_priority.get(c, ord(c)) for c in x[1]])):
                    if not depends:
                        continue
                    if depends in already_added:
                        continue
                    extra = conditions.get("extra", set[str]())
                    if extra and pypi_test_extras.intersection(extra):
                        continue
                    already_added.add(depends)
                    already_added.add(f"{requirement}[${{PYTHON_USEDEP}}]")
                    ebuild.write(f"\t{depends}\n")
                if single_impl:
                    ebuild.write("')")
                ebuild.write('"\n')
                done["Requires"] = True
                if not generated_depend:
                    append_generated("RDEPEND", "GENERATED_RDEPEND", line)
            elif has_requirements and not done["RDEPEND"] and 'RDEPEND="' in line and done["Requires"]:
                append_generated("RDEPEND", "GENERATED_RDEPEND", line)
            elif has_extras and not done["extras"] and "IUSE=" in line:
                generated_iuse = "GENERATED_IUSE=" in line
                multiline = odd_quote and generated_iuse
                ebuild.write(f'GENERATED_IUSE="{" ".join(sorted(extras))}"\n')
                done["extras"] = True
                if not generated_iuse:
                    append_generated("IUSE", "GENERATED_IUSE", line)
            elif has_extras and 'IUSE="' in line and done["extras"]:
                multiline = odd_quote
                if not done["IUSE"]:
                    append_generated("IUSE", "GENERATED_IUSE", line)
            elif not has_extras and 'GENERATED_IUSE=' in line:
                multiline = odd_quote
            elif not has_extras and 'IUSE=' in line and "${GENERATED_IUSE}" in line:
                ebuild.write(line.replace("${GENERATED_IUSE} ", "").replace("${GENERATED_IUSE}", "").rstrip() + '\n')
            elif (has_tests or pypi_sdist_ext != ".tar.gz") and not done["GENERATED_BDEPEND"] \
                    and "distutils_enable_tests" in line:
                ebuild.write(line.rstrip() + '\n')
                ebuild.write('GENERATED_BDEPEND="${BDEPEND}\n')
                if pypi_sdist_ext != ".tar.gz":
                    ebuild.write('\tapp-arch/unzip\n')
                if has_tests:
                    ebuild.write('\ttest? (\n')
                    already_added = set[str]()
                    for requirement, depends, conditions in \
                            sorted(requirements, key=lambda x: (x[0], [symbol_priority.get(c, ord(c)) for c in x[1]])):
                        if not depends:
                            continue
                        if depends in already_added:
                            continue
                        extra = conditions.get("extra", set[str]())
                        if not extra or not pypi_test_extras.intersection(extra):
                            continue
                        already_added.add(depends)
                        already_added.add(f"{requirement}[${{PYTHON_USEDEP}}]")
                        ebuild.write(f"\t\t{depends}\n")
                    ebuild.write('\t)\n')
                ebuild.write('"\n')
                if not done["BDEPEND"]:
                    ebuild.write('BDEPEND="${GENERATED_BDEPEND}"\n')
                    done["BDEPEND"] = True
                done["GENERATED_BDEPEND"] = True
            elif (has_tests or pypi_sdist_ext != ".tar.gz") and 'BDEPEND="' in line and done["BDEPEND"]:
                cleaned_line = line
                for remove in {"GENERATED_BDEPEND", "BDEPEND"}:
                    cleaned_line = cleaned_line.replace('${GENERATED_' + remove + '} ', '')
                    cleaned_line = cleaned_line.replace('${GENERATED_' + remove + '}', '')
                if cleaned_line.strip() != 'BDEPEND=""':
                    ebuild.write(cleaned_line.replace('BDEPEND="', 'BDEPEND+=" ').rstrip() + '\n')
            elif (has_tests or pypi_sdist_ext != ".tar.gz") and 'BDEPEND+=" test? (' in line \
                    and done["GENERATED_BDEPEND"]:
                multiline = odd_quote
            elif (has_tests or pypi_sdist_ext != ".tar.gz") and 'GENERATED_BDEPEND="' in line \
                    and done["GENERATED_BDEPEND"]:
                multiline = odd_quote
            elif has_license and not done["License"] and "LICENSE=" in line:
                multiline = odd_quote
                ebuild.write('LICENSE="' + output["License"] + '"\n')
                done["License"] = True
            elif not done["Summary"] and "DESCRIPTION=" in line:
                multiline = odd_quote
                ebuild.write('DESCRIPTION="' + output["Summary"].replace(r'"', r'\"').replace(r'`', r'\`') + '"\n')
                done["Summary"] = True
            elif not done["Project-URLs"] and "HOMEPAGE=" in line:
                multiline = odd_quote
                ebuild.write('HOMEPAGE="\n  https://pypi.org/project/{}/{}"\n'
                             .format(pypi_package, output["Project-URLs"].replace(r'"', r'\"').replace(r'`', r'\`')))
                done["Project-URLs"] = True
            elif pypi_p and "PYPI_PN=" in line:
                multiline = odd_quote
                skip_empty_lines = True
            elif pypi_p and 'PYPI_NO_NORMALIZE=' in line:
                multiline = odd_quote
                skip_empty_lines = True
            elif pypi_p and "SRC_URI=" in line and import_pypi:
                multiline = odd_quote
                skip_empty_lines = True
            elif pypi_p and line.startswith('S=') and import_pypi:
                multiline = odd_quote
                skip_empty_lines = True
            elif pypi_p and (not done["PYPI_PN"] or not done["SRC_URI"]) and "inherit" in line and "pypi" in line:
                import_pypi = True
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
                    ebuild.write(f'S=\"${{WORKDIR}}/{pypi_pn}-{pypi_pv}\"\n')
                done["SRC_URI"] = True
                ebuild.write('\n')
                skip_empty_lines = True
            elif pypi_p and (not done["PYPI_PN"] or not done["SRC_URI"]) and "inherit" in line and "pypi" not in line:
                # Assume that if we inherit without pypi, SRC_URI and S are manually set
                import_pypi = False
                ebuild.write(line.rstrip() + '\n')
            elif not done["KEYWORDS"] and "KEYWORDS=" in line:
                multiline = odd_quote
                ebuild.write('KEYWORDS="amd64 arm64"\n')
                done["KEYWORDS"] = True
            elif not done["PYTHON_COMPAT"] and "PYTHON_COMPAT=" in line:
                ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n")
                done["PYTHON_COMPAT"] = True
            elif "could not be inserted in this ebuild" in line or "# Content: " in line:
                skip_empty_lines = True
            elif "# Content: " in line:
                skip_commented_or_empty_lines = True
            elif "GENERATED_" in line:
                if "GENERATED_DEPEND=" in line:
                    multiline = odd_quote
                    skip_empty_lines = True
                else:
                    cleaned_line = line
                    for remove in {"RDEPEND", "BDEPEND", "DEPEND", "IUSE"}:
                        cleaned_line = cleaned_line.replace(f'${{GENERATED_{remove}}} ', '')
                        cleaned_line = cleaned_line.replace(f'${{GENERATED_{remove}}}', '')
                    ebuild.write(cleaned_line.rstrip() + '\n')
            else:
                ebuild.write(line.rstrip() + '\n')

        if not import_pypi:
            done["PYPI_PN"] = True
            done["SRC_URI"] = True

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

    for module_dep, module_names in gather_modules().items():
        for module_name in module_names:
            deptree[module_name].add(module_dep)

    for core_dep in core_requirements():
        deptree["homeassistant.core"].add(core_dep)

    # homeassistant-base/ha-core as a dependency on dev-python/homeassistant.
    # ha-core will have dependencies to specific (frozen) versions while homeassistant will follow pip's requirements
    deptree["homeassistant.core"].add("homeassistant==" + homeassistant_version)

    def module_task(module: str, deps: set[str]) -> None:
        tokenized_module = trailing_numbers.sub(r'\1', module).split('.')
        if len(tokenized_module) > 1 and tokenized_module[0] == "homeassistant":
            tokenized_module[0] = "ha"
        if len(tokenized_module) > 2 and tokenized_module[1] == "components":
            tokenized_module[1] = "comp"
        gentoo_module = "-".join(tokenized_module).replace(".", "-").replace("_", "-")
        module_deps = []
        with ThreadPoolExecutor() as executor:
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

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(module_task, module, deps): module for module, deps in deptree.items()}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"Error in gen_homeassistant_ebuilds for {futures[future]}: {e}", file=sys.stderr)
                traceback.print_exc()

    # TODO: should be an option, as we might want to keep old ebuilds sometimes
    global treated_packages_lock
    global treated_packages
    with treated_packages_lock:
        # protect some special files:
        treated_packages.add('metadata/md5-cache')
        treated_packages.add('acct-group/homeassistant')
        treated_packages.add('acct-user/homeassistant')

        # delete everything else
        for folder in overlay_dir.glob('*/*/'):
            rel_path = str(folder.relative_to(overlay_dir)).rstrip('/')
            if rel_path not in treated_packages:
                shutil.rmtree(folder)


gen_homeassistant_ebuilds()
