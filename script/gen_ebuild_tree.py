#!/bin/python

from script.gen_requirements_all import gather_modules, core_requirements
from collections import defaultdict
import re
from pathlib import Path

deptree = defaultdict(lambda: defaultdict(set))

for module, topics in gather_modules().items():
  for topic in topics:
    token = topic.split('.')
    assert len(token) > 2
    assert token[0] == "homeassistant"
    deptree[".".join(token[1:-1])][token[-1]].add(module)

tokenizer = re.compile(r'([^\[\]<>=]+)(?:\[([^\[\]]+)\])?((?:[<>=]=?[^\[\]<>=]+)+)')
tokenizerEq = re.compile(r'==([^<>=,]+)')
tokenizerGt = re.compile(r'>([^<>=,]+)')
tokenizerGe = re.compile(r'>=([^<>=,]+)')
tokenizerLt = re.compile(r'<([^<>=,]+)')
tokenizerLe = re.compile(r'<=([^<>=,]+)')

print("# Home Assistant Core dependencies")
print(r'RDEPEND="${RDEPEND}')
for coredep in sorted(core_requirements(), key=lambda dep: [s.casefold() if s is not None else "" for s in tokenizer.match(dep).group(1,3,2)] ):
  # print('#', coredep)
  depToken = tokenizer.match(coredep)
  name = "dev-python/" + depToken[1]
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
  print("\t" + ' '.join(coreStr))
print(r'"')

for topic, modules in deptree.items():
  print('#', topic)
  print(r'RDEPEND="${RDEPEND}')
  for module, deps in sorted(modules.items()):
    # print("#", deps)
    moduleStr = "\t" + module + "? ( "
    for dep in sorted(deps, key=lambda dep: [s.casefold() if s is not None else "" for s in tokenizer.match(dep).group(1,3,2)]):
      depToken = tokenizer.match(dep)
      name = "dev-python/" + depToken[1]
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
      ebuildDir = Path("/var/db/repos/gentoo-homeassistant/" + name)
      skelPath = Path("gentoo/tree_skel/dev-python.ebuild")
      if verEq is not None:
        moduleStr += "~" + name + '-' + verEq[1] + use + ' '
        ebuildDir.mkdir(parents=True, exist_ok=True)
        with ebuildDir.joinpath(depToken[1] + '-' + verEq[1] + ".ebuild").open("w") as ebuild, skelPath.open("r") as skel:
          ebuild.write(skel.read())
      if verGt is not None:
        moduleStr += ">" + name + '-' + verGt[1] + use + ' '
      if verGe is not None:
        moduleStr += ">=" + name + '-' + verGe[1] + use + ' '
      if verLt is not None:
        moduleStr += "<" + name + '-' + verLt[1] + use + ' '
      if verLe is not None:
        moduleStr += "<=" + name + '-' + verLe[1] + use + ' '
    moduleStr += ")"
    print(moduleStr)
  print(r'"')
