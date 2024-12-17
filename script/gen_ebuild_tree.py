#!/bin/python

from script.gen_requirements_all import gather_modules, core_requirements
from collections import defaultdict
import re
from pathlib import Path

import subprocess
import sys
import os

# TODO: detect this somehow
homeassistant_version = "2024.12.1"
overlay_dir = Path("/var/db/repos/gentoo-homeassistant")
gentoo_overlay = Path("/var/db/repos/gentoo")


def digest(ebuild_path: Path) -> None:
    args = ["sudo", "ebuild", str(ebuild_path.absolute()), "digest"]
    try:
        subprocess.check_call(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + ": failed returncode={}".format(e.returncode), file=sys.stderr)


treated_packages = set()
# Gentoo uses https://github.com/projg2/certifi-system-store to use the system store.
# Let's use the package from gentoo tree.
treated_packages.add("dev-python/certifi")

pip_show_split = re.compile(br'([^: ]+): ?(.*)')
get_revision = re.compile(r'-r([0-9]+)$')
# TODO: allow different names for both pypi and gentoo, maybe even version alias (TBC)
pypi_package_alias = defaultdict(str)
pypi_package_alias["jaraco.net"] = "jaraco_net"  # version 10.2.1 tarball does not match name...
pypi_package_alias["AEMET-OpenData"] = "aemet_opendata"  # version 0.6.3
pypi_license_override = defaultdict(str)
pypi_license_override["zwave-js-server-python"] = "Apache-2.0"
pypi_license_override["numpy"] = "BSD"
ebuild_path_override = defaultdict(Path)
ebuild_path_override["dev-python/async-timeout"] = gentoo_overlay.joinpath("dev-python/async-timeout")\
    .joinpath("async-timeout-4.0.3.ebuild")
ebuild_path_override["dev-python/aemet-opendata"] = Path("/var/db/repos/HomeAssistantRepository/")\
    .joinpath("dev-python/AEMET-OpenData/").joinpath("AEMET-OpenData-0.5.4.ebuild")
ebuild_category_override = defaultdict(str)
ebuild_category_override["geopy"] = "sci-geosciences"


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

    global treated_packages
    treated_packages_len = len(treated_packages)
    treated_packages.add(gentoo_package_full)
    if treated_packages_len == len(treated_packages):
        return gentoo_package_full

    # TODO: add a mode to early exit if the ebuild exists

    output = defaultdict(str)

    args = [sys.executable, "-m", "pip", "show", "--verbose", gentoo_package]
    try:
        raw_output = subprocess.check_output(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + ": failed returncode={}".format(e.returncode), file=sys.stderr)
        return gentoo_package_full

    last_key = ""
    for output_line in raw_output.split(b'\n'):
        output_pair = pip_show_split.fullmatch(output_line)
        if output_pair is None:
            output[last_key] += '\n' + output_line.decode("utf-8")
        else:
            last_key = output_pair.group(1).decode("utf-8")
            assert last_key not in output
            output[last_key] = output_pair.group(2).decode("utf-8")

    version = output["Version"].replace(".post", "_p")
    version_revision = 0
    if pypi_package in pypi_license_override:
        output["License"] = pypi_license_override[pypi_package]
    has_license = output["License"] != ""

    for revision in gentoo_overlay.joinpath(gentoo_package_full).glob(
            gentoo_package + "-" + version + "*.ebuild"):
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

    requirements = []
    for requirement in output["Requires"].split(", "):
        if requirement:
            requirements += [gen_python_ebuild(requirement)]
    has_requirements = len(requirements) > 0

    # TODO: let the overlay path be configurable
    python_ebuild_dir = overlay_dir.joinpath(gentoo_package_full)
    if gentoo_package_full in ebuild_path_override:
        skel_path = ebuild_path_override[gentoo_package_full]
    else:
        args = ["equery", "w", gentoo_package_full]
        skel_path = None
        try:
            skel_path = Path(subprocess.check_output(args, env=dict(os.environ, ACCEPT_KEYWORDS="amd64 arm64"))
                             .decode("utf-8").split('\n')[0])
        except subprocess.CalledProcessError:
            pass

        if skel_path is None:
            try:
                skel_path = Path(subprocess.check_output(args, env=dict(os.environ, ACCEPT_KEYWORDS="~amd64 ~arm64"))
                                 .decode("utf-8").split('\n')[0])
            except subprocess.CalledProcessError:
                pass

        if skel_path is None:
            # This is a bare minimum fallback for when we can't find a better ebuild.
            # Chances are you'll have to tweak it
            # TODO: do we need a warning here? There are high chances this ebuild will need more attention
            skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    python_ebuild_dir.mkdir(parents=True, exist_ok=True)
    ebuild_path = python_ebuild_dir.joinpath(gentoo_package + '-' + version + version_revision + ".ebuild")
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
                ebuild.write("GENERATED_DEPEND=\"\n")
                for requirement in requirements:
                    ebuild.write("\t" + requirement + r'[${PYTHON_USEDEP}]' + "\n")
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
                ebuild.write("DESCRIPTION=\"" + output["Summary"].replace(r'"', r'\"') + "\"\n")
                done["Summary"] = True
            elif not done["Project-URLs"] and "HOMEPAGE=" in line:
                multiline = odd_quote
                ebuild.write("HOMEPAGE=\"\n  https://pypi.org/project/{}/{}\"\n"
                             .format(pypi_package, output["Project-URLs"].replace(r'"', r'\"')))
                done["Project-URLs"] = True
            elif package_mismatch and "PYPI_PN=" in line:
                multiline = odd_quote
                ebuild.write('PYPI_PN="' + pypi_package + '"\n')
                done["PYPI_PN"] = True
            elif package_mismatch and not done["PYPI_PN"] and "inherit" in line and "pypi" in line:
                ebuild.write('PYPI_PN="' + pypi_package + '"\n\n')
                ebuild.write(line)
                done["PYPI_PN"] = True
            elif not done["KEYWORDS"] and "KEYWORDS=" in line:
                multiline = odd_quote
                ebuild.write("KEYWORDS=\"amd64 arm64\"\n")
                done["KEYWORDS"] = True
            else:
                ebuild.write(line)

        for key, value in done.items():
            assert value, key + " could not be inserted in ebuild"

    digest(ebuild_path)
    return gentoo_package_full


def gen_homeassistant_ebuilds() -> None:
    metadata_dir = overlay_dir.joinpath("metadata")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    with metadata_dir.joinpath("layout.conf").open("w") as layout:
        layout.write("masters = gentoo")

    profile_dir = overlay_dir.joinpath("profiles")
    profile_dir.mkdir(parents=True, exist_ok=True)
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

    tokenizer = re.compile(r'([^\[\]<>=]+)(?:\[([^\[\]]+)])?((?:[<>=]=?[^\[\]<>=]+)+)')
    tokenizer_eq = re.compile(r'==([^<>=,]+)')
    tokenizer_gt = re.compile(r'>([^<>=,]+)')
    tokenizer_ge = re.compile(r'>=([^<>=,]+)')
    tokenizer_lt = re.compile(r'<([^<>=,]+)')
    tokenizer_le = re.compile(r'<=([^<>=,]+)')

    # TODO: parallelize?
    for module, deps in deptree.items():
        tokenized_module = module.split('.')
        assert len(tokenized_module) > 2
        assert tokenized_module[0] == "homeassistant"
        tokenized_module[0] = "ha"
        if tokenized_module[1] == "components":
            tokenized_module[1] = "comp"
        gentoo_module = "-".join(tokenized_module).replace(".", "-").replace("_", "-")
        ebuild_dir = overlay_dir.joinpath("homeassistant-base").joinpath(gentoo_module)
        ebuild_dir.mkdir(parents=True, exist_ok=True)
        ebuild_path = ebuild_dir.joinpath(gentoo_module + "-" + homeassistant_version + ".ebuild")
        with ebuild_path.open("w") as ebuild:
            ebuild.write("EAPI=8\n\n")
            ebuild.write("PYTHON_COMPAT=( python3_{12,13,13t} )\n\n")
            ebuild.write("inherit python-r1\n\n")
            ebuild.write("DESCRIPTION=\"Home Assistant Meta-Package " + module + "\"\n")
            ebuild.write("LICENSE=\"Apache-2.0\"\n\n")
            ebuild.write("SLOT=\"0\"\n")
            ebuild.write("KEYWORDS=\"amd64 arm64\"\n\n")
            ebuild.write(r'RDEPEND="' + '\n')
            for dep in sorted(deps, key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                          tokenizer.match(dep_name).group(1, 3, 2)]):
                dep_token = tokenizer.match(dep)

                name = gen_python_ebuild(dep_token[1])
                use = r'['
                if dep_token[2] is not None:
                    use += dep_token[2] + r','
                use += r'${PYTHON_USEDEP}]'
                ver = dep_token[3]
                module_str = []
                ver_eq = tokenizer_eq.search(ver)
                ver_gt = tokenizer_gt.search(ver)
                ver_ge = tokenizer_ge.search(ver)
                ver_lt = tokenizer_lt.search(ver)
                ver_le = tokenizer_le.search(ver)
                if ver_eq is not None:
                    module_str += ["~" + name + '-' + ver_eq[1].replace(".post", "_p") + use]
                if ver_gt is not None:
                    module_str += [">" + name + '-' + ver_gt[1].replace(".post", "_p") + use]
                if ver_ge is not None:
                    module_str += [">=" + name + '-' + ver_ge[1].replace(".post", "_p") + use]
                if ver_lt is not None:
                    module_str += ["<" + name + '-' + ver_lt[1].replace(".post", "_p") + use]
                if ver_le is not None:
                    module_str += ["<=" + name + '-' + ver_le[1].replace(".post", "_p") + use]
                ebuild.write("\t" + ' '.join(module_str) + '\n')
            ebuild.write(r'"' + '\n')
        digest(ebuild_path)


gen_homeassistant_ebuilds()
