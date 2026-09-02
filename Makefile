# The three targets arch-repo's packaging contract asks for. The contract is at
# https://github.com/aaronsb/arch-repo/blob/main/docs/packaging-contract.md and
# a worked example is beside it under docs/example.
#
# There is deliberately no aur target. arch-repo watches this repository, reads
# ./PKGBUILD from the default branch, takes the version and checksum from the
# newest published release, and pushes to the AUR and the [aaronsb] pacman
# repository. A second writer to the same AUR ref is how a PKGBUILD and its
# .SRCINFO drift apart.

NAME    := $(shell sed -n 's/^pkgname=//p' PKGBUILD)
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)

.PHONY: help check package release version test lint

help: ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t20

check: version lint test ## Everything CI would run

# The ruff version is part of the gate, not incidental to it: the default rule
# selection grows between minor releases, which is how this project reached 244
# findings without a commit causing them. pyproject pins the range for anyone
# installing .[dev], but `make check` runs whatever ruff is on PATH, so that is
# what gets checked. Moving to a newer series is a deliberate act — bump this,
# then clear whatever the new defaults surface.
RUFF_SERIES := 0.16

lint:
	@command -v ruff >/dev/null || { echo "needs ruff $(RUFF_SERIES).x" >&2; exit 1; }
	@v=$$(ruff --version | awk '{print $$2}'); \
	  case "$$v" in \
	    $(RUFF_SERIES).*) ;; \
	    *) echo "ruff $$v on PATH, gate is calibrated for $(RUFF_SERIES).x." >&2; \
	       echo "A different series changes the default rule set. Either use" >&2; \
	       echo "$(RUFF_SERIES).x, or bump RUFF_SERIES here and in pyproject and" >&2; \
	       echo "clear what the new defaults find." >&2; \
	       exit 1;; \
	  esac
	ruff check src tests

test:
	pytest -q

# PKGBUILD's pkgver is a placeholder that arch-repo overwrites, so it is not one
# of the values compared here. What has to agree is the tag about to be cut and
# the version the software reports about itself. Reporting rather than failing:
# before a release the tag is legitimately absent, and after one it is
# legitimately present, so neither state is an error on its own.
version: ## Report the version this repository would release
	@test -n "$(VERSION)" || { echo "no version in pyproject.toml" >&2; exit 1; }
	@test -n "$(NAME)"    || { echo "no pkgname in PKGBUILD" >&2; exit 1; }
	@if git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null; then \
	    echo "$(NAME) $(VERSION) — v$(VERSION) is already tagged"; \
	else \
	    echo "$(NAME) $(VERSION) — not yet tagged; this is what the next release will be"; \
	fi

package: version ## Build ./PKGBUILD in a clean chroot and namcap it
	@command -v extra-x86_64-build >/dev/null || { echo "needs devtools" >&2; exit 1; }
	@command -v updpkgsums >/dev/null        || { echo "needs pacman-contrib" >&2; exit 1; }
	@command -v namcap >/dev/null            || { echo "needs namcap" >&2; exit 1; }
	rm -rf build && mkdir -p build
	# The tarball the release would carry, built from HEAD. Named exactly what
	# the recipe's source= resolves to, so makepkg finds it and never reaches
	# for the published archive — which does not exist until the tag does, and
	# would make a pre-release dry run impossible.
	git archive --format=tar.gz --prefix=$(NAME)-$(VERSION)/ \
	    -o build/$(NAME)-$(VERSION).tar.gz HEAD
	cp PKGBUILD $(wildcard *.install) build/
	cd build && sed -i 's/^pkgver=.*/pkgver=$(VERSION)/' PKGBUILD && updpkgsums
	cd build && extra-x86_64-build
	# namcap exits 0 whether or not it found errors, so its output is what
	# decides. This mirrors arch-repo's gate: errors fail, warnings do not, and
	# a package may allow specific errors by listing regexes in .namcap-allow.
	# Debug packages are excluded because every .build-id entry in one is a
	# symlink into the main package, which namcap reports as dangling.
	cd build && namcap PKGBUILD $$(ls ./*.pkg.tar.zst | grep -v -- '-debug-') | tee namcap.txt
	@cd build && if [ -f ../.namcap-allow ]; then \
	    bad=$$(grep ' E: ' namcap.txt | grep -vE -f ../.namcap-allow || true); \
	  else \
	    bad=$$(grep ' E: ' namcap.txt || true); \
	  fi; \
	  if [ -n "$$bad" ]; then echo "namcap errors:"; printf '%s\n' "$$bad"; exit 1; fi; \
	  echo "namcap: no errors"

release: ## Produce the artifacts a release must carry
	@echo "nothing to build — the recipe reads the source tarball GitHub generates"
