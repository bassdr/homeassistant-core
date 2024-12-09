#!/bin/python

from script.gen_requirements_all import gather_modules, core_requirements
from collections import defaultdict
import re
from pathlib import Path

import subprocess
import sys

homeassistant_version = "2024.12.1"


def digest(ebuild_path):
    args = ["echo", "sudo", "ebuild", ebuild_path, "digest"]
    try:
        subprocess.check_call(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + ": failed returncode={}".format(e.returncode), file=sys.stderr)


treated_packages = set()
pip_show_split = re.compile(r'([^: ]+): ?(.*)')


def gen_ebuild(package):
    global treated_packages
    treated_packages_len = len(treated_packages)
    treated_packages.add(package)
    if treated_packages_len == len(treated_packages):
        return

    output = defaultdict(str)
    args = [sys.executable, "-m", "pip", "show", "--verbose", package]
    try:
        raw_output = subprocess.check_output(args)
    except subprocess.CalledProcessError as e:
        print(" ".join(args) + ": failed returncode={}".format(e.returncode), file=sys.stderr)
        return

    last_key = ""
    for output_line in raw_output.decode("utf-8").split('\n'):
        output_pair = pip_show_split.fullmatch(output_line)
        if output_pair is None:
            output[last_key] += '\n' + output_line
        else:
            last_key = output_pair.group(1)
            output[last_key] = output_pair.group(2)

    version = output["Version"]

    python_ebuild_dir = Path("/var/db/repos/gentoo-homeassistant/dev-python/" + package)
    args = ["equery", "w", "dev-python/" + package]
    try:
        skel_path = Path(subprocess.check_output(args).decode("utf-8").split('\n')[0])
    except subprocess.CalledProcessError:
        skel_path = Path("gentoo/tree_skel/dev-python.ebuild")

    python_ebuild_dir.mkdir(parents=True, exist_ok=True)
    with python_ebuild_dir.joinpath(package + '-' + version + ".ebuild").open("w") as ebuild, skel_path.open("r") as skel:
        print("Creating " + ebuild.name)
        ebuild.write(skel.read())
        for requirement in output["Requires"].split(", "):
            ebuild.write("# " + requirement + '\n')
            gen_ebuild(requirement.lower().replace(".", "-"))
        digest(ebuild.name)


deptree = defaultdict(lambda: defaultdict(set))

for module, topics in gather_modules().items():
    for topic in topics:
        token = topic.split('.')
        assert len(token) > 2
        assert token[0] == "homeassistant"
        deptree[".".join(token[1:-1])][token[-1]].add(module)

tokenizer = re.compile(r'([^\[\]<>=]+)(?:\[([^\[\]]+)])?((?:[<>=]=?[^\[\]<>=]+)+)')
tokenizerEq = re.compile(r'==([^<>=,]+)')
tokenizerGt = re.compile(r'>([^<>=,]+)')
tokenizerGe = re.compile(r'>=([^<>=,]+)')
tokenizerLt = re.compile(r'<([^<>=,]+)')
tokenizerLe = re.compile(r'<=([^<>=,]+)')

ebuildDir = Path("/var/db/repos/gentoo-homeassistant/homeassistant-base/ha-core")
ebuildDir.mkdir(parents=True, exist_ok=True)
with ebuildDir.joinpath("ha-core-" + homeassistant_version + ".ebuild").open("w") as ebuild:
    ebuild.write("# Home Assistant Core dependencies" + '\n')
    ebuild.write(r'RDEPEND="${RDEPEND}' + '\n')
    for coredep in sorted(core_requirements(), key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                                     tokenizer.match(dep_name).group(1, 3, 2)]):
        # print('#', coredep)
        depToken = tokenizer.match(coredep)
        # gen_ebuild(depToken[1])
        name = "dev-python/" + depToken[1].lower().replace(".", "-").replace("_", "-")
        use = r'['
        if depToken[2] is not None:
            use += depToken[2] + r','
        use += r'${PYTHON_USEDEP}]'
        ver = depToken[3]
        verEq = tokenizerEq.search(ver)
        verGt = tokenizerGt.search(ver)
        verGe = tokenizerGe.search(ver)
        verLt = tokenizerLt.search(ver)
        verLe = tokenizerLe.search(ver)
        coreStr = []
        if verEq is not None:
            coreStr += ["~" + name + '-' + verEq[1] + use]
        if verGt is not None:
            coreStr += [">" + name + '-' + verGt[1] + use]
        if verGe is not None:
            coreStr += [">=" + name + '-' + verGe[1] + use]
        if verLt is not None:
            coreStr += ["<" + name + '-' + verLt[1] + use]
        if verLe is not None:
            coreStr += ["<=" + name + '-' + verLe[1] + use]
        ebuild.write("\t" + ' '.join(coreStr) + '\n')
    ebuild.write(r'"' + '\n')

gen_ebuild("homeassistant")

for topic, modules in deptree.items():
    for module, deps in sorted(modules.items()):
        module_name = "-".join(["ha", topic, module]).lower().replace(".", "-").replace("_", "-")
        ebuildDir = Path("/var/db/repos/gentoo-homeassistant/homeassistant-base/" + module_name)
        ebuildDir.mkdir(parents=True, exist_ok=True)
        with ebuildDir.joinpath(module_name + "-" + homeassistant_version + ".ebuild").open("w") as ebuild:
            ebuild.write("# Home Assistant Core dependencies" + '\n')
            ebuild.write(r'RDEPEND="${RDEPEND}' + '\n')
            for dep in sorted(deps, key=lambda dep_name: [s.casefold() if s is not None else "" for s in
                                                          tokenizer.match(dep_name).group(1, 3, 2)]):
                depToken = tokenizer.match(dep)
                short_name = depToken[1].lower().replace(".", "-").replace("_", "-")
                gen_ebuild(short_name)
                name = "dev-python/" + short_name
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
                    moduleStr += ["~" + name + '-' + verEq[1] + use]
                if verGt is not None:
                    moduleStr += [">" + name + '-' + verGt[1] + use]
                if verGe is not None:
                    moduleStr += [">=" + name + '-' + verGe[1] + use]
                if verLt is not None:
                    moduleStr += ["<" + name + '-' + verLt[1] + use]
                if verLe is not None:
                    moduleStr += ["<=" + name + '-' + verLe[1] + use]
                ebuild.write("\t" + ' '.join(moduleStr) + '\n')
            ebuild.write(r'"' + '\n')
