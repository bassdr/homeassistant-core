# Copyright 1999-2024 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DISTUTILS_EXT=1
DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_{11..13} )

inherit distutils-r1 pypi

DESCRIPTION="@DESCRIPTION@"
HOMEPAGE="@HOMEPAGE@"

LICENSE="@LICENSE@"
SLOT="0"
KEYWORDS="amd64 arm arm64 x86"

RDEPEND="@RDEPEND@"
BDEPEND="${RDEPEND} @BDEPEND@"

EPYTEST_XDIST=1
distutils_enable_tests pytest
