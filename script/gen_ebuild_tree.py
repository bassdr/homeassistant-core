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


def digest(ebuild_path):
    args = ["sudo", "ebuild", str(ebuild_path), "digest"]
    try:
        subprocess.check_call(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + ": failed returncode={}".format(e.returncode), file=sys.stderr)


treated_packages = set()
# Gentoo uses https://github.com/projg2/certifi-system-store to use the system store.
# Let's use the package from gentoo tree.
treated_packages.add("dev-python/certifi")

pip_show_split = re.compile(br'([^: ]+): ?(.*)')
quote_count = re.compile(r'(^|[^\\])"')
# TODO: allow different names for both pypi and gentoo, maybe even version alias (TBC)
pypi_package_alias = defaultdict(str)
pypi_package_alias["jaraco.net"] = "jaraco_net"  # version 10.2.1 tarball does not match name...


def gen_python_ebuild(pypi_package):
    if pypi_package in pypi_package_alias:
        pypi_package = pypi_package_alias[pypi_package]

    gentoo_package = pypi_package.lower().replace(".", "-").replace("_", "-")
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
    has_license = output["License"] != ""

    requirements = []
    for requirement in output["Requires"].split(", "):
        if requirement:
            requirements += [gen_python_ebuild(requirement)]
    has_requirements = len(requirements) > 0

    # TODO: let the overlay path be configurable
    python_ebuild_dir = overlay_dir.joinpath(gentoo_package_full)
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
        # This is a bare minimum fallback for when we can't find a better ebuild. Chances are you'll have to tweak it
        # TODO: do we need a warning here? There are high chances this ebuild will need more attention
        skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    python_ebuild_dir.mkdir(parents=True, exist_ok=True)
    ebuild_path = python_ebuild_dir.joinpath(gentoo_package + '-' + version + ".ebuild")
    with ebuild_path.open("w") as ebuild, skel_path.open("r") as old_ebuild:
        print("Creating " + ebuild.name)

        done = defaultdict(bool)
        multiline = False
        package_mismatch = gentoo_package != pypi_package
        for line in old_ebuild:
            odd_quote = len(quote_count.findall(line)) % 2 == 1
            if multiline:
                multiline = not odd_quote
            elif has_requirements and not done["RDEPEND"] and "RDEPEND=\"" in line and done["Requires"]:
                ebuild.write(line.replace("RDEPEND=\"", "RDEPEND=\"${GENERATED_DEPEND} "))
                done["RDEPEND"] = True
            elif has_requirements and not done["RDEPEND"] and "RDEPEND=" in line and done["Requires"]:
                ebuild.write(line.replace("RDEPEND=", "RDEPEND=\"${GENERATED_DEPEND}\""))
                done["RDEPEND"] = True
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


def gen_homeassistant_ebuilds():
    metadata_dir = overlay_dir.joinpath("metadata")
    metadata_dir.mkdir(parents=True, exist_ok=True)
    with metadata_dir.joinpath("layout.conf").open("w") as layout:
        layout.write("masters = gentoo")

    profile_dir = overlay_dir.joinpath("profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    with profile_dir.joinpath("categories").open("w") as categories:
        categories.write("homeassistant-base")

    deptree = defaultdict(set)

    for module, topics in gather_modules().items():
        for topic in topics:
            tokens = topic.split('.')
            assert len(tokens) > 2
            assert tokens[0] == "homeassistant"
            tokens[0] = "ha"
            if tokens[1] == "components":
                tokens[1] = "comp"
            deptree["-".join(tokens).lower().replace("_", "-")].add(module)

    for core_dep in core_requirements():
        deptree["ha-core"].add(core_dep)

    # homeassistant-base/ha-core as a dependency on dev-python/homeassistant.
    # ha-core will have dependencies to specific versions while homeassistant will follow pip's requirements
    deptree["ha-core"].add("homeassistant==" + homeassistant_version)

    tokenizer = re.compile(r'([^\[\]<>=]+)(?:\[([^\[\]]+)])?((?:[<>=]=?[^\[\]<>=]+)+)')
    tokenizerEq = re.compile(r'==([^<>=,]+)')
    tokenizerGt = re.compile(r'>([^<>=,]+)')
    tokenizerGe = re.compile(r'>=([^<>=,]+)')
    tokenizerLt = re.compile(r'<([^<>=,]+)')
    tokenizerLe = re.compile(r'<=([^<>=,]+)')

    # TODO: parallelize?
    for module, deps in deptree.items():
        # TODO: let the overlay path be configurable
        ebuildDir = overlay_dir.joinpath("homeassistant-base").joinpath(module)
        ebuildDir.mkdir(parents=True, exist_ok=True)
        ebuild_path = ebuildDir.joinpath(module + "-" + homeassistant_version + ".ebuild")
        with ebuild_path.open("w") as ebuild:
            ebuild.write("EAPI=8\n\n")
            ebuild.write("PYTHON_COMPAT=( python3_{12,13,13t} pypy3 )\n\n")
            ebuild.write("inherit python-any-r1\n\n")
            ebuild.write("DESCRIPTION=\"Home Assistant Meta-Package " + module + "\"\n")
            ebuild.write("LICENSE=\"Apache-2.0\"\n\n")
            ebuild.write("SLOT=\"0\"\n")
            ebuild.write("KEYWORDS=\"amd64 arm64\"\n\n")
            ebuild.write(r'RDEPEND="' + '\n')
            for dep in sorted(deps, key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                          tokenizer.match(dep_name).group(1, 3, 2)]):
                depToken = tokenizer.match(dep)

                name = gen_python_ebuild(depToken[1])
                use = r'['
                if depToken[2] is not None:
                    use += depToken[2] + r','
                use += r'${PYTHON_USEDEP}]'
                ver = depToken[3]
                moduleStr = []
                verEq = tokenizerEq.search(ver)
                verGt = tokenizerGt.search(ver)
                verGe = tokenizerGe.search(ver)
                verLt = tokenizerLt.search(ver)
                verLe = tokenizerLe.search(ver)
                if verEq is not None:
                    moduleStr += ["~" + name + '-' + verEq[1].replace(".post", "_p") + use]
                if verGt is not None:
                    moduleStr += [">" + name + '-' + verGt[1].replace(".post", "_p") + use]
                if verGe is not None:
                    moduleStr += [">=" + name + '-' + verGe[1].replace(".post", "_p") + use]
                if verLt is not None:
                    moduleStr += ["<" + name + '-' + verLt[1].replace(".post", "_p") + use]
                if verLe is not None:
                    moduleStr += ["<=" + name + '-' + verLe[1].replace(".post", "_p") + use]
                ebuild.write("\t" + ' '.join(moduleStr) + '\n')
            ebuild.write(r'"' + '\n')
        digest(ebuild_path)


gen_homeassistant_ebuilds()
