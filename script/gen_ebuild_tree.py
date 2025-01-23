#!/bin/python

import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests

from .gen_requirements_all import gather_modules, core_requirements

# TODO: detect this somehow
homeassistant_version = "2024.12.1"
overlay_dir = Path("/var/db/repos/gentoo-homeassistant")
gentoo_overlay = Path("/var/db/repos/gentoo")
multi_thread = False


fetch_pypi_metadata_lock = threading.Lock()
fetch_pypi_metadata_cache = dict[str, requests.Response]()


def fetch_pypi_metadata(package_name: str, package_version: str = "") -> {}:
    package_name = package_name.strip()
    package_version = package_version.strip()
    url = f'https://pypi.org/pypi/{package_name}{"/" + package_version if package_version else ""}/json'

    global fetch_pypi_metadata_lock
    global fetch_pypi_metadata_cache
    with fetch_pypi_metadata_lock:
        if url in fetch_pypi_metadata_cache:
            response = fetch_pypi_metadata_cache[url]
        else:
            response = requests.get(url)
            fetch_pypi_metadata_cache[url] = response

    if response.status_code == 200:
        return response.json()

    print(f'Error: Unable to fetch metadata for {package_name}'
          f'{" " + package_version if package_version else ""} (status code {response.status_code})', file=sys.stderr)
    return {}


def fetch_pypi_versions(package_name: str) -> list[str]:
    return fetch_pypi_metadata(package_name).get("releases", {}).keys()


def fetch_pypi_latest_version(package_name: str) -> str:
    return fetch_pypi_metadata(package_name).get("info", {}).get("version", "")


def fetch_pypi_requires_dist(package_name: str, package_version: str = "") -> list[str]:
    return fetch_pypi_metadata(package_name, package_version).get("info", {}).get("requires_dist", [])


def fetch_pypi_license(package_name: str, package_version: str = "") -> str:
    return fetch_pypi_metadata(package_name, package_version).get("info", {}).get("license", "")


def manifest_ebuild(ebuild_path: Path) -> None:
    args = ["sudo", "ebuild", str(ebuild_path.absolute()), "manifest"]
    print(" ".join(args))
    return_code = subprocess.call(args)
    if return_code != 0:
        print(f": failed with return_code={return_code}", file=sys.stderr)


treated_packages_lock = threading.Lock()
treated_packages = set[str]()

pip_show_split = re.compile(br'([^: ]+): ?(.*)')
get_revision = re.compile(r'-r([0-9]+)\.ebuild$')
trailing_numbers = re.compile(r'[-_]([0-9]+)$')

pypi_package_alias = dict[str, str]()
pypi_package_alias["asyncio_dgram"] = "asyncio-dgram"  # version 2.2.0
pypi_package_alias["anel-pwrctrl-homeassistant"] = "anel_pwrctrl-homeassistant"  # version 0.0.1.dev2
pypi_package_alias["incomfort-client"] = "incomfort_client"  # version 0.6.3.post1
pypi_package_alias["aiohttp_sse_client2"] = "aiohttp-sse-client2"  # version 0.3.0
pypi_package_alias["pure_pcapy3"] = "pure-pcapy3"  # version 1.0.1
pypi_package_alias["certifi-system-store"] = "certifi"  # version 2024.12.14, using the gentoo fork
pypi_package_alias["pyegps"] = "pyEGPS"  # version 0.2.5
pypi_package_alias["pypasser"] = "PyPasser"  # version 0.0.5
pypi_package_alias["pyelectra"] = "pyElectra"  # version 1.2.4
pypi_package_alias["rx"] = "Rx"  # version 3.2.0
pypi_package_alias["umodbus"] = "uModbus"  # version 1.0.4

pypi_package_version_alias = dict[str, str]()
pypi_package_version_alias["pybbox-0.0.5a0"] = "0.0.5-alpha"
pypi_package_version_alias["certifi-2024.12.14"] = "3024.7.22"
pypi_package_version_alias["certifi-2024.8.30"] = "3024.7.22"
pypi_package_version_alias["libsoundtouch-0.8"] = "0.8.0"
pypi_package_version_alias["bcrypt-4.2.0"] = "4.2.1"
pypi_package_version_alias["uv-0.5.4"] = "0.5.6"
pypi_package_version_alias["pyairvisual-2023.08.1"] = "2023.8.1"
pypi_package_version_alias["aioambient-2024.08.0"] = "2024.8.0"

pypi_zip_suffix = set[str]()
pypi_zip_suffix.add("pybbox-0.0.5-alpha")
pypi_zip_suffix.add("python-family-hub-local-0.0.2")
pypi_zip_suffix.add("nsapi-3.0.5")
pypi_zip_suffix.add("config-0.5.1")
pypi_zip_suffix.add("vultr-0.1.2")
pypi_zip_suffix.add("azure-eventhub-5.11.1")
pypi_zip_suffix.add("azure-servicebus-7.10.0")
pypi_zip_suffix.add("psychrolib-2.5.0")

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

gentoo_licenses = os.listdir(gentoo_overlay.joinpath("licenses"))


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
replace_alpha = re.compile(r'a([0-9]+)')
replace_beta = re.compile(r'b([0-9]+)')
replace_rc = re.compile(r'rc([0-9]+)')


def normalize_version(ver: str) -> str:
    ver = replace_alpha.sub(r"_alpha\1", ver)
    ver = replace_beta.sub(r"_beta\1", ver)
    ver = replace_rc.sub(r"_rc\1", ver)
    ver = ver.replace(r".post", r"_p")
    # .devN releases would be closer to gentoo's -rN, but it's reserved for ebuild releases.
    # Using _p here instead. Crossing fingers we'll not fall on a combination of those eventually...
    ver = ver.replace(r".dev", r"_p")
    ver = remove_v.sub(r"\1", ver)
    ver = trailing_numbers.sub(r"_p\1", ver)  # not sure... this one is not documented...
    ver = ver.replace(r"-alpha", "_alpha")
    ver = ver.replace(r"-beta", "_beta")
    ver = ver.replace(r"-rc", "_rc")
    ver = ver.replace(r".*", r"*")
    ver = ver.replace(r"**", r"*")
    return ver.strip().rstrip(r'.').rstrip(r'-')


tokenizer = re.compile(r'\s*([^\[\]~<>=!()\s,]+)'  # Name of the package or special keyword like extra or python_version
                       r'\s*(?:\[([^\[\]~<>=!()]+)])?'  # Extras required for this dependency
                       r'\s*\(?\s*((?:[~<>=!]=?\s*[^\[\]~<>=!()\s,]+\s*)+)?\s*\)?\s*')  # Version requirements
tokenizer_eq = re.compile(r'[=~]=\s*["\']?([^<>=,"\']+)["\']?')
tokenizer_gt = re.compile(r'>\s*["\']?([^<>=,"\']+)["\']?')
tokenizer_ge = re.compile(r'>=\s*["\']?([^<>=,"\']+)["\']?')
tokenizer_lt = re.compile(r'<\s*["\']?([^<>=,"\']+)["\']?')
tokenizer_le = re.compile(r'<=\s*["\']?([^<>=,"\']+)["\']?')
tokenizer_ne = re.compile(r'!=\s*["\']?([^<>=,"\']+)["\']?')


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

    def full_gentoo_depend(version: str = "") -> str:
        version = version.strip()
        if version and pypi_package + '-' + version in pypi_package_version_alias:
            version = pypi_package_version_alias[pypi_package + '-' + version]
        elif pypi_package in pypi_package_version_alias:
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
            gentoo_package_depends += ["~" + full_gentoo_depend(pypi_package_version_eq[1])]
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

    gentoo_package_condition = defaultdict[str, set[str]](set[str])
    for condition in pypi_requires_split[1:]:
        pypi_condition_tokenized = tokenizer.match(condition)
        if pypi_condition_tokenized is not None and \
                pypi_condition_tokenized[1] is not None and pypi_condition_tokenized[3] is not None:
            pypi_condition_tokenized_eq = tokenizer_eq.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_eq is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add(pypi_condition_tokenized_eq[1])
            pypi_condition_tokenized_gt = tokenizer_gt.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_gt is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add('>' + pypi_condition_tokenized_gt[1])
            pypi_condition_tokenized_ge = tokenizer_ge.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_ge is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add('>=' + pypi_condition_tokenized_ge[1])
            pypi_condition_tokenized_lt = tokenizer_lt.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_lt is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add('<' + pypi_condition_tokenized_lt[1])
            pypi_condition_tokenized_le = tokenizer_le.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_le is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add('<=' + pypi_condition_tokenized_le[1])
            pypi_condition_tokenized_ne = tokenizer_ne.search(pypi_condition_tokenized[3])
            if pypi_condition_tokenized_ne is not None:
                gentoo_package_condition[pypi_condition_tokenized[1]].add('!=' + pypi_condition_tokenized_ne[1])

    # tuple that will be returned, contains information for the parent ebuild that depends on this ebuild.
    gentoo_package: tuple[str, str, dict[str, set[str]]] = (
        gentoo_package_name,
        gentoo_package_depends,
        gentoo_package_condition
    )

    global treated_packages_lock
    with treated_packages_lock:
        global treated_packages
        treated_packages_len = len(treated_packages)
        treated_packages.add(gentoo_package_name)
        if treated_packages_len == len(treated_packages):
            return gentoo_package

    output = defaultdict[str, str](str)

    args = [sys.executable, "-m", "pip", "show", "--verbose", pypi_package]
    try:
        raw_output = subprocess.check_output(args)
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
    if pypi_package_version and pypi_package + '-' + pypi_package_version in pypi_package_version_alias:
        pypi_package_version = pypi_package_version_alias[pypi_package + '-' + output["Version"]]
    elif pypi_package in pypi_package_version_alias:
        pypi_package_version = pypi_package_version_alias[pypi_package]

    if not pypi_package_version:
        pypi_package_version = fetch_pypi_latest_version(pypi_package)

    # I'm giving up, I can't find a version to generate. The ebuild will have to be created manually to resolve the deps
    if not pypi_package_version:
        return gentoo_package

    gentoo_package_version = normalize_version(pypi_package_version)
    zip_suffix = pypi_package + '-' + pypi_package_version in pypi_zip_suffix or pypi_package in pypi_zip_suffix
    version_mismatch = gentoo_package_version != pypi_package_version

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
        args = ["equery", "w", gentoo_package_name]
        skel_path = None
        try:
            skel_path = Path(subprocess.check_output(args, env=dict(os.environ, ACCEPT_KEYWORDS="amd64 arm64"))
                             .decode(sys.stdout.encoding).split('\n')[0])
        except subprocess.CalledProcessError:
            pass

    if skel_path is None:
        try:
            skel_path = Path(subprocess.check_output(args, env=dict(os.environ, ACCEPT_KEYWORDS="~amd64 ~arm64"))
                             .decode(sys.stdout.encoding).split('\n')[0])
        except subprocess.CalledProcessError:
            pass

    if skel_path is None:
        # This is a bare minimum fallback for when we can't find a better ebuild.
        # Chances are you'll have to tweak it
        skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    if skel_path.parent.joinpath("files").exists() and not python_ebuild_dir.joinpath("files").exists():
        shutil.copytree(skel_path.parent.joinpath("files"), python_ebuild_dir.joinpath("files"), dirs_exist_ok=True)

    requires_dist = fetch_pypi_requires_dist(pypi_package, pypi_package_version)
    if requires_dist is None:
        requires_dist = list[str]()
    for require in output["Requires"].split(","):
        require = require.strip()
        if require and require not in requires_dist:
            requires_dist += [require]

    #if requires_dist is not None:
        # Append to requires the dependencies found in pypi
        #requires += requires_dist

        #dep_token = dep.split(';python_version')
        #python_version = ''
        #if len(dep_token) > 1:
        #    python_version = dep_token[1]
        #python_condition = ("", "")
        #if python_version:
        #    if python_version.startswith("<") and "3.13" in python_version:
        #        python_condition = (r"$(python_gen_cond_dep '", r"' python3_12)")
        #    elif python_version.startswith(">=") and "3.13" in python_version:
        #        python_condition = (r"$(python_gen_cond_dep '", r"' python3_13{,t})")
        #    else:
        #        python_condition = (r"$(python_gen_cond_dep '", fr"' {python_version})")

        #for pypi_dependency in requires_dist:
        #    packages = []
        #    extras = []
        #    for dep in pypi_dependency.split(';'):
        #        pypi_requires_tokenized = tokenizer.match(dep)
        #        if pypi_requires_tokenized is not None and \
        #                pypi_requires_tokenized[1] is not None and \
        #                pypi_requires_tokenized[3] is not None and \
        #                pypi_requires_tokenized[1].strip(" '\"(),") == "extra":
        #            extras += [pypi_requires_tokenized[3].strip(" '\"()=<>,")]
        #        else:
        #            packages += [dep]
        #    if len(extras) <= 0:
        #        requires += packages
        #    for extra in extras:
        #        extra_requires[extra] += packages

    # Recursively call this function for all requirements
    requirements = list[tuple[str, str, dict[str, set[str]]]]()
    if multi_thread:
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(gen_python_ebuild, req): req for req in requires_dist if req is not None}
            for future in as_completed(futures):
                try:
                    requirements = future.result()
                except Exception as e:
                    print(f"Error processing {futures[future]}: {e}", file=sys.stderr)
                    traceback.print_exc()
                    raise
    else:
        requirements = [gen_python_ebuild(req) for req in requires_dist if req is not None]

    has_requirements = len(requirements) > 0
    extras = set[str]()
    for requirement, depends, condition in requirements:
        if "extra" in condition:
            extras.add(condition["extra"])
    has_extras = len(extras) > 0

    update_ebuild = ebuild_path == skel_path
    if update_ebuild:
        skel_path = skel_path.rename(skel_path.parent.joinpath(skel_path.name + ".tmp"))

    with ebuild_path.open("w") as ebuild, skel_path.open("r") as old_ebuild:
        print("Creating " + ebuild.name)

        done = defaultdict[str, bool](bool)
        skip_empty_lines = False
        multiline = False
        package_mismatch = gentoo_package_short_name != pypi_package

        def append_generated(variable: str, generated_variable: str, input_line: str) -> None:
            nonlocal done, ebuild
            generated_variable = "${" + generated_variable + "}"
            if f'{variable}=""' in input_line:
                ebuild.write(input_line.replace(f'{variable}=""', f'{variable}="{generated_variable}"').rstrip() + '\n')
            elif generated_variable not in input_line:
                ebuild.write(input_line.replace(f'{variable}="', f'{variable}="{generated_variable} ').rstrip() + '\n')
            else:
                ebuild.write(input_line.rstrip() + '\n')
            done[variable] = True

        for line in old_ebuild:
            if skip_empty_lines and (not line.strip() or line.startswith('#')):
                continue
            else:
                skip_empty_lines = False

            odd_quote = count_quotes(line) % 2 == 1
            if multiline:
                multiline = not odd_quote
            elif has_requirements and not done["Requires"] and "DEPEND=" in line:
                generated_depend = "GENERATED_DEPEND=" in line
                multiline = odd_quote and generated_depend
                ebuild.write('GENERATED_DEPEND="\n')
                for requirement, depends, condition in sorted(requirements):
                    if depends:
                        ebuild.write("\t" + depends + "\n")
                ebuild.write('"\n')
                done["Requires"] = True
                if not generated_depend:
                    append_generated("RDEPEND", "GENERATED_DEPEND", line)
            elif has_requirements and not done["RDEPEND"] and 'RDEPEND="' in line and done["Requires"]:
                append_generated("RDEPEND", "GENERATED_DEPEND", line)
            elif has_extras and not done["extras"] and "IUSE=" in line:
                generated_iuse = "GENERATED_IUSE=" in line
                multiline = odd_quote and generated_iuse
                ebuild.write(f'GENERATED_IUSE="{" ".join(sorted(extras))}"\n')
                done["extras"] = True
                if not generated_iuse:
                    append_generated("IUSE", "GENERATED_IUSE", line)
            elif has_extras and not done["IUSE"] and 'IUSE="' in line and done["extras"]:
                append_generated("IUSE", "GENERATED_IUSE", line)
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
            elif package_mismatch and "PYPI_PN=" in line:
                multiline = odd_quote
                if not done["PYPI_PN"]:
                    ebuild.write('PYPI_PN="' + pypi_package + '"\n')
                    skip_empty_lines = True
                    done["PYPI_PN"] = True
            elif (version_mismatch or zip_suffix) and done["SRC_URI"] and "SRC_URI=" in line or \
                    version_mismatch and done["SRC_URI"] and line.startswith('S='):
                multiline = odd_quote
                skip_empty_lines = True
            elif (package_mismatch and not done["PYPI_PN"] or
                  ((version_mismatch or zip_suffix) and not done["SRC_URI"])) and "inherit" in line and "pypi" in line:
                if package_mismatch and not done["PYPI_PN"]:
                    ebuild.write('PYPI_PN="' + pypi_package + '"\n')
                    done["PYPI_PN"] = True
                ebuild.write(line)
                if version_mismatch or zip_suffix:
                    ebuild.write('\nSRC_URI="$(pypi_sdist_url --no-normalize "' + pypi_package +
                                 '" "' + pypi_package_version + '"')
                    if zip_suffix:
                        ebuild.write(' ".zip"')
                    ebuild.write(')"\n')

                if version_mismatch:
                    ebuild.write('S="${WORKDIR}/' + pypi_package + '-' + pypi_package_version + '"\n')

                if version_mismatch or zip_suffix:
                    ebuild.write('\n')
                    done["SRC_URI"] = True
                    skip_empty_lines = True
            elif not done["KEYWORDS"] and "KEYWORDS=" in line:
                multiline = odd_quote
                ebuild.write('KEYWORDS="amd64 arm64"\n')
                done["KEYWORDS"] = True
            elif not done["PYTHON_COMPAT"] and "PYTHON_COMPAT=" in line:
                ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n")
                done["PYTHON_COMPAT"] = True
            elif "could not be inserted in this ebuild" in line or "# Content: " in line:
                skip_empty_lines = True
            else:
                ebuild.write(line)

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

        ebuild_dir = overlay_dir.joinpath("homeassistant-base").joinpath(gentoo_module)
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
            ebuild.write(r'RDEPEND="\n')
            for dep in sorted(deps, key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                          tokenizer.match(dep_name).group(1, 3, 2)]):
                name, depends, condition = gen_python_ebuild(dep)
                if depends:
                    ebuild.write("\t" + depends + '\n')
            ebuild.write(r'"' + '\n')
        manifest_ebuild(ebuild_path)

    if multi_thread:
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(module_task, module, deps): module for module, deps in deptree.items()}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"Error in module_task for {futures[future]}: {e}")
                    traceback.print_exc()
                    raise
    else:
        for module, deps in deptree.items():
            module_task(module, deps)


gen_homeassistant_ebuilds()
