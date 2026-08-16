# The three targets arch-repo's packaging contract asks for. The contract is at
# https://github.com/aaronsb/arch-repo/blob/main/docs/packaging-contract.md and
# a worked example is beside it under docs/example.
#
# There is deliberately no aur target. arch-repo watches this repository, reads
# ./PKGBUILD from the default branch, takes the version and checksum from the
# newest published release, and pushes to the AUR and the [aaronsb] pacman
# repository. A second writer to the same AUR ref is how a PKGBUILD and its
# .SRCINFO drift apart.

VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)

.PHONY: check package release version-guard test lint

## Everything CI would run
check: version-guard lint test

lint:
	ruff check src tests

test:
	pytest -q

# PKGBUILD's pkgver is a placeholder that arch-repo overwrites, so it is not one
# of the values compared here. What has to agree is the tag about to be cut and
# the version the software reports about itself.
version-guard:
	@test -n "$(VERSION)" || { echo "no version in pyproject.toml" >&2; exit 1; }
	@if git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null; then \
	    echo "v$(VERSION) is already tagged"; \
	else \
	    echo "v$(VERSION) is not yet tagged — this is what the next release will be"; \
	fi

## Build ./PKGBUILD in a clean chroot and namcap it — the pre-release dry run of
## what arch-repo will do, so a broken recipe fails here rather than in a bump PR
package:
	@command -v extra-x86_64-build >/dev/null || { echo "needs devtools" >&2; exit 1; }
	@command -v updpkgsums >/dev/null        || { echo "needs pacman-contrib" >&2; exit 1; }
	rm -rf build && mkdir -p build
	cp PKGBUILD playtimed.install build/
	cd build \
	  && sed -i 's/^pkgver=.*/pkgver=$(VERSION)/' PKGBUILD \
	  && updpkgsums \
	  && extra-x86_64-build \
	  && namcap PKGBUILD ./*.pkg.tar.zst

## Produce the artifacts a release must carry. Nothing: the recipe reads the
## source tarball GitHub generates for every release.
release:
	@echo "nothing to build — the recipe reads GitHub's generated source tarball"
