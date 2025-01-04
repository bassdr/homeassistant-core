#!/bin/python

from .gen_requirements_all import gather_modules, core_requirements
from collections import defaultdict
import re
from pathlib import Path
import subprocess
import sys
import os
from concurrent.futures import ThreadPoolExecutor
import threading
import requests
import shutil

# TODO: detect this somehow
homeassistant_version = "2024.12.1"
overlay_dir = Path("/var/db/repos/gentoo-homeassistant")
gentoo_overlay = Path("/var/db/repos/gentoo")


def fetch_pypi_metadata(package_name):
    url = f'https://pypi.org/pypi/{package_name}/json'
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error: Unable to fetch metadata for {package_name} (status code {response.status_code})")
        return None


def check_ebuild(ebuild_path: Path) -> bool:
    if not ebuild_path.exists():
        return False

    env = dict(os.environ, PORTDIR_OVERLAY=str(ebuild_path.parent.parent.parent.absolute()))
    args = ["sudo", "ebuild", str(ebuild_path.absolute()), "fetch"]
    print(" ".join(args))
    if subprocess.call(args, env=env) != 0:
        return False

    versioned_package = '=' + '/'.join(ebuild_path.parts[i] for i in [-3, -1]).removesuffix(".ebuild")
    args = ["emerge", "--pretend", "--quiet", versioned_package]
    print(" ".join(args))
    if subprocess.call(args, env=env) != 0:
        return False

    return True


def manifest_ebuild(ebuild_path: Path) -> None:
    args = ["sudo", "ebuild", str(ebuild_path.absolute()), "manifest"]
    print(" ".join(args))
    return_code = subprocess.call(args)
    if return_code != 0:
        print(f": failed with return_code={return_code}", file=sys.stderr)


treated_packages_lock = threading.Lock()
treated_packages = set()

with treated_packages_lock:
    # Gentoo uses https://github.com/projg2/certifi-system-store to use the system store.
    # Let's use the package from gentoo tree to avoid security issues.
    treated_packages.add("dev-python/certifi")
    # These are using a standalone pep517. Let's use gentoo's for now.
    treated_packages.add("dev-python/pyyaml")
    treated_packages.add("dev-python/pillow")

pip_show_split = re.compile(br'([^: ]+): ?(.*)')
get_revision = re.compile(r'-r([0-9]+)$')
# TODO: allow different names for both pypi and gentoo, maybe even version alias (TBC)
pypi_package_alias = defaultdict(str)
pypi_package_alias["jaraco.net"] = "jaraco_net"  # version 10.2.1 tarball does not match name...
pypi_package_alias["AEMET-OpenData"] = "aemet_opendata"  # version 0.6.3
pypi_package_alias["anel-pwrctrl-homeassistant"] = "anel_pwrctrl-homeassistant"  # version 0.0.1.dev2
pypi_package_alias["fake-useragent"] = "fake_useragent"  # version 2.0.1
pypi_package_alias["azure-core"] = "azure_core"  # version 1.32.0
pypi_package_alias["azure-identity"] = "azure_identity"  # version 1.19.0
pypi_package_alias["azure-storage-blob"] = "azure_storage_blob"  # version 1.19.0
#pypi_package_alias["azure-servicebus"] = "azure_servicebus"  # version 1.19.0
pypi_package_alias["azure-eventhub"] = "azure_eventhub"  # version 1.19.0
pypi_package_alias["yt-dlp"] = "yt_dlp"  # version 2024.12.3
pypi_package_alias["pymicro-vad"] = "pymicro_vad"  # version 1.0.1
pypi_package_alias["pyspeex-noise"] = "pyspeex_noise"  # version 1.0.2
pypi_package_alias["python-didl-lite"] = "python_didl_lite"  # version 1.4.1

pypi_package_version = defaultdict(str)
pypi_package_version["pybbox-0.0.5a0"] = "0.0.5_alpha"
ebuild_path_override = defaultdict(Path)
ebuild_path_override["dev-python/async-timeout"] = gentoo_overlay.joinpath("dev-python/async-timeout") \
    .joinpath("async-timeout-4.0.3.ebuild")
ebuild_path_override["dev-python/aemet-opendata"] = Path("/var/db/repos/HomeAssistantRepository/") \
    .joinpath("dev-python/AEMET-OpenData/").joinpath("AEMET-OpenData-0.5.4.ebuild")
ebuild_path_override["dev-python/huggingface-hub"] = gentoo_overlay.joinpath("sci-libs/huggingface_hub") \
    .joinpath("/huggingface_hub-0.24.7.ebuild")
ebuild_path_override["dev-python/sharp-aquos-rc"] = Path("/var/db/repos/HomeAssistantRepository/") \
    .joinpath("dev-python/sharp_aquos_rc/sharp_aquos_rc-0.3.2.ebuild")
ebuild_path_override["dev-python/pyric"] = Path("/var/db/repos/HomeAssistantRepository/") \
    .joinpath("dev-python/PyRIC/PyRIC-0.1.6.3.ebuild")
ebuild_category_override = defaultdict(str)
ebuild_category_override["geopy"] = "sci-geosciences"
ebuild_category_override["tokenizers"] = "sci-libs"
ebuild_category_override["acme"] = "app-crypt"
ebuild_no_use_python = set()
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


def gen_python_ebuild(pypi_package: str) -> str:
    if pypi_package in pypi_package_alias:
        pypi_package = pypi_package_alias[pypi_package]

    gentoo_package = pypi_package.lower().replace(".", "-").replace("_", "-")
    if pypi_package in ebuild_category_override:
        gentoo_package_full = ebuild_category_override[pypi_package] + "/" + gentoo_package
    else:
        gentoo_package_full = "dev-python/" + gentoo_package

    with treated_packages_lock:
        global treated_packages
        treated_packages_len = len(treated_packages)
        treated_packages.add(gentoo_package_full)
        if treated_packages_len == len(treated_packages):
            return gentoo_package_full

    output = defaultdict(str)

    args = [sys.executable, "-m", "pip", "show", "--verbose", pypi_package]
    try:
        raw_output = subprocess.check_output(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + f": failed returncode={e.returncode}", file=sys.stderr)
        return gentoo_package_full

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

    # .devN releases would be closer to gentoo's -rN, but it's reserved for ebuild releases.
    # Using _p here instead. Crossing fingers we'll not fall on a combination of those eventually...
    gentoo_version = output["Version"] \
        .replace("a", "_alpha") \
        .replace("b", "_beta") \
        .replace("rc", "_rc") \
        .replace(".post", "_p") \
        .replace(".dev", "_p")

    if pypi_package + '-' + output["Version"] in pypi_package_version:
        output["Version"] = pypi_package_version[pypi_package + '-' + output["Version"]]

    version_mismatch = gentoo_version != output["Version"]

    version_revision = 0
    has_license = output["License"] in gentoo_licenses

    for revision in gentoo_overlay.joinpath(gentoo_package_full).glob(
            gentoo_package + "-" + gentoo_version + "*.ebuild"):
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

    requires = [require.strip() for require in output["Requires"].split(",") if require.strip()]
    has_requirements = len(requires) > 0

    if has_requirements:
        with ThreadPoolExecutor() as executor:
            requirements = set(executor.map(gen_python_ebuild, requires))

    # TODO: let the overlay path be configurable
    python_ebuild_dir = overlay_dir.joinpath(gentoo_package_full)
    if gentoo_package_full in ebuild_path_override:
        skel_path = ebuild_path_override[gentoo_package_full]
    else:
        args = ["equery", "w", gentoo_package_full]
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
            # TODO: do we need a warning here? There are high chances this ebuild will need more attention
            skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    python_ebuild_dir.mkdir(parents=True, exist_ok=True)
    ebuild_path = python_ebuild_dir.joinpath(gentoo_package + '-' + gentoo_version + version_revision + ".ebuild")

    if skel_path.parent.joinpath("files").exists() and python_ebuild_dir != skel_path.parent:
        shutil.copytree(skel_path.parent.joinpath("files"), python_ebuild_dir.joinpath("files"), dirs_exist_ok=True)

    if check_ebuild(ebuild_path):
        return gentoo_package_full

    update_ebuild = ebuild_path == skel_path
    if update_ebuild:
        skel_path = skel_path.rename(skel_path.parent.joinpath(skel_path.name + ".tmp"))

    with ebuild_path.open("w") as ebuild, skel_path.open("r") as old_ebuild:
        print("Creating " + ebuild.name)

        done = defaultdict(bool)
        multiline = False
        package_mismatch = gentoo_package != pypi_package
        for line in old_ebuild:
            odd_quote = count_quotes(line) % 2 == 1
            if multiline:
                multiline = not odd_quote
            elif has_requirements and not done["Requires"] and "DEPEND=" in line:
                generated_depend = "GENERATED_DEPEND=" in line
                multiline = odd_quote and generated_depend
                python_usedep = r'[${PYTHON_USEDEP}]'
                ebuild.write("GENERATED_DEPEND=\"\n")
                for requirement in requirements:
                    if requirement in ebuild_no_use_python:
                        ebuild.write("\t" + requirement + "\n")
                    else:
                        ebuild.write("\t" + requirement + python_usedep + "\n")
                ebuild.write("\"\n")
                done["Requires"] = True
                if not generated_depend:
                    if not done["RDEPEND"] and "RDEPEND=\"" in line:
                        ebuild.write(line.replace("RDEPEND=\"", "RDEPEND=\"${GENERATED_DEPEND} "))
                        done["RDEPEND"] = True
                    elif not done["RDEPEND"] and "RDEPEND=" in line:
                        ebuild.write(line.replace("RDEPEND=", "RDEPEND=\"${GENERATED_DEPEND}\""))
                        done["RDEPEND"] = True
                    else:
                        ebuild.write(line)
            elif has_requirements and not done["RDEPEND"] and "RDEPEND=\"" in line and done["Requires"]:
                ebuild.write(line.replace("${GENERATED_DEPEND}", "")
                             .replace("RDEPEND=\"", "RDEPEND=\"${GENERATED_DEPEND} "))
                done["RDEPEND"] = True
            elif has_requirements and not done["RDEPEND"] and "RDEPEND=" in line and done["Requires"]:
                ebuild.write(line.replace("${GENERATED_DEPEND}", "")
                             .replace("RDEPEND=", "RDEPEND=\"${GENERATED_DEPEND}\""))
                done["RDEPEND"] = True
            elif has_license and not done["License"] and "LICENSE=" in line:
                multiline = odd_quote
                ebuild.write("LICENSE=\"" + output["License"] + "\"\n")
                done["License"] = True
            elif not done["Summary"] and "DESCRIPTION=" in line:
                multiline = odd_quote
                ebuild.write("DESCRIPTION=\"" + output["Summary"].replace(r'"', r'\"').replace(r'`', r'\`') + "\"\n")
                done["Summary"] = True
            elif not done["Project-URLs"] and "HOMEPAGE=" in line:
                multiline = odd_quote
                ebuild.write("HOMEPAGE=\"\n  https://pypi.org/project/{}/{}\"\n"
                             .format(pypi_package, output["Project-URLs"].replace(r'"', r'\"').replace(r'`', r'\`')))
                done["Project-URLs"] = True
            elif package_mismatch and "PYPI_PN=" in line:
                multiline = odd_quote
                ebuild.write('PYPI_PN="' + pypi_package + '"\n')
                done["PYPI_PN"] = True
            elif version_mismatch and ("SRC_URI=" in line or line.startswith('S=')):
                multiline = odd_quote
            elif ((package_mismatch and not done["PYPI_PN"]) or (version_mismatch and not done["SRC_URI"])) \
                    and "inherit" in line and "pypi" in line:
                if package_mismatch:
                    ebuild.write('PYPI_PN="' + pypi_package + '"\n\n')
                    done["PYPI_PN"] = True
                ebuild.write(line)
                if version_mismatch:
                    ebuild.write('\nSRC_URI="$(pypi_sdist_url --no-normalize "' + pypi_package +
                                 '" "' + output["Version"] + '")"\n'
                                                             'S="${WORKDIR}/' + pypi_package + '-' + output[
                                     "Version"] + '"\n')
                    done["SRC_URI"] = True
            elif not done["KEYWORDS"] and "KEYWORDS=" in line:
                multiline = odd_quote
                ebuild.write("KEYWORDS=\"amd64 arm64\"\n")
                done["KEYWORDS"] = True
            elif not done["PYTHON_COMPAT"] and "PYTHON_COMPAT" in line:
                ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n")
                done["PYTHON_COMPAT"] = True
            else:
                ebuild.write(line)

        for key, value in done.items():
            if not value:
                ebuild.write(f"\n# {key} could not be inserted in this ebuild\n")
                if key in output:
                    ebuild.write("# Content: " + "\n# ".join(output[key].split('\n')) + "\n")

    if update_ebuild:
        skel_path.unlink()

    manifest_ebuild(ebuild_path)
    return gentoo_package_full


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

    deptree = defaultdict(set)

    for module_dep, module_names in gather_modules().items():
        for module_name in module_names:
            deptree[module_name].add(module_dep)

    for core_dep in core_requirements():
        deptree["homeassistant.core"].add(core_dep)

    # homeassistant-base/ha-core as a dependency on dev-python/homeassistant.
    # ha-core will have dependencies to specific (frozen) versions while homeassistant will follow pip's requirements
    deptree["homeassistant.core"].add("homeassistant==" + homeassistant_version)

    trailing_numbers = re.compile(r'[-_]([0-9]+)$')
    tokenizer = re.compile(r'([^\[\]<>=]+)(?:\[([^\[\]]+)])?((?:[<>=]=?[^\[\]<>=]+)+)')
    tokenizer_eq = re.compile(r'==([^<>=,]+)')
    tokenizer_gt = re.compile(r'>([^<>=,]+)')
    tokenizer_ge = re.compile(r'>=([^<>=,]+)')
    tokenizer_lt = re.compile(r'<([^<>=,]+)')
    tokenizer_le = re.compile(r'<=([^<>=,]+)')

    remove_v = re.compile(r'v([0-9]+)')
    replace_alpha = re.compile(r'a([0-9]+)')
    replace_beta = re.compile(r'b([0-9]+)')
    replace_rc = re.compile(r'rc([0-9]+)')

    def module_task(module, deps):
        tokenized_module = trailing_numbers.sub(r'\1', module).split('.')
        if len(tokenized_module) > 1 and tokenized_module[0] == "homeassistant":
            tokenized_module[0] = "ha"
        if len(tokenized_module) > 2 and tokenized_module[1] == "components":
            tokenized_module[1] = "comp"
        gentoo_module = "-".join(tokenized_module).replace(".", "-").replace("_", "-")

        ebuild_dir = overlay_dir.joinpath("homeassistant-base").joinpath(gentoo_module)
        ebuild_dir.mkdir(parents=True, exist_ok=True)
        ebuild_path = ebuild_dir.joinpath(gentoo_module + "-" + homeassistant_version + ".ebuild")

        if check_ebuild(ebuild_path):
            return

        with ebuild_path.open("w") as ebuild:
            ebuild.write("EAPI=8\n\n")
            ebuild.write("PYTHON_COMPAT=( python3_{12,13{,t}} )\n\n")
            ebuild.write("inherit python-r1\n\n")
            ebuild.write("DESCRIPTION=\"Home Assistant Meta-Package " + module + "\"\n")
            ebuild.write("LICENSE=\"Apache-2.0\"\n\n")
            ebuild.write("SLOT=\"0\"\n")
            ebuild.write("KEYWORDS=\"amd64 arm64\"\n\n")
            ebuild.write(r'RDEPEND="' + '\n')
            for dep in sorted(deps, key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                          tokenizer.match(dep_name).group(1, 3, 2)]):
                dep_token = dep.split(';python_version')
                python_version = ''
                if len(dep_token) > 1:
                    python_version = dep_token[1]
                dep_token = tokenizer.match(dep_token[0])

                python_condition = ("", "")
                if python_version:
                    breakpoint()
                    if python_version.startswith("<") and "3.13" in python_version:
                        python_condition = (r"$(python_gen_cond_dep '", r"' python3_12)")
                    elif python_version.startswith(">=") and "3.13" in python_version:
                        python_condition = (r"$(python_gen_cond_dep '", r"' python3_13{,t})")
                    else:
                        python_condition = (r"$(python_gen_cond_dep '", fr"' {python_version})")

                if gentoo_module == "ha-comp-anthropic":
                    breakpoint()

                name = gen_python_ebuild(dep_token[1])

                if gentoo_module == "ha-comp-anthropic":
                    breakpoint()

                use = []
                if dep_token[2] is not None:
                    use += [dep_token[2]]

                if name not in ebuild_no_use_python:
                    use += [r"${PYTHON_USEDEP}"]

                if len(use) > 0:
                    use = "[" + ",".join(use) + "]"
                else:
                    use = ''

                ver = dep_token[3]
                ver = replace_alpha.sub(r"_alpha\1", ver)
                ver = replace_beta.sub(r"_beta\1", ver)
                ver = replace_rc.sub(r"_rc\1", ver)
                ver = ver.replace(r".post", r"_p")
                ver = ver.replace(r".dev", r"_p")
                ver = remove_v.sub(r"\1", ver)
                ver = trailing_numbers.sub(r"_p\1", ver)  # not sure... this one is not documented...
                ver = ver.replace(r"-alpha", "_alpha")
                ver = ver.replace(r"-beta", "_beta")
                ver = ver.replace(r"-rc", "_rc")
                module_str = []
                ver_eq = tokenizer_eq.search(ver)
                ver_gt = tokenizer_gt.search(ver)
                ver_ge = tokenizer_ge.search(ver)
                ver_lt = tokenizer_lt.search(ver)
                ver_le = tokenizer_le.search(ver)
                if ver_eq is not None:
                    module_str += ["~" + name + '-' + ver_eq[1] + use]
                if ver_gt is not None:
                    module_str += [">" + name + '-' + ver_gt[1] + use]
                if ver_ge is not None:
                    module_str += [">=" + name + '-' + ver_ge[1] + use]
                if ver_lt is not None:
                    module_str += ["<" + name + '-' + ver_lt[1] + use]
                if ver_le is not None:
                    module_str += ["<=" + name + '-' + ver_le[1] + use]
                ebuild.write("\t" + python_condition[0] + ' '.join(module_str) + python_condition[1] + '\n')
            ebuild.write(r'"' + '\n')
        manifest_ebuild(ebuild_path)

    with ThreadPoolExecutor() as executor:
        executor.map(lambda item: module_task(item[0], item[1]), deptree.items())


gen_homeassistant_ebuilds()
