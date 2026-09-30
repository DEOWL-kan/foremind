"""Dependency manifests (DESIGN §8.1 #3/#4): the one list the gate's diff check and the hooks' #4 paths share."""
from fnmatch import fnmatchcase

# Sections are dotted prefixes: (runtime -> #4, dev -> #3). Other changed keys that look like dependencies, and every
# other manifest, need #4 ("cannot parse = #4", §8.2).
SECTIONS = {
    "package.json": (("dependencies", "peerDependencies", "optionalDependencies"), ("devDependencies",)),
    "pyproject.toml": (("project.dependencies", "project.optional-dependencies"), ("dependency-groups",)),
}
# Patterns with a `/` match the whole repo path (fnmatch's `*` crosses `/`), the others the file name.
MANIFESTS = (*SECTIONS, "requirements*.txt", "requirements*.in", "*requirements/*.txt", "*requirements/*.in",
             "constraints*.txt", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock", "pdm.lock", "pixi.toml",
             "setup.py", "setup.cfg", "environment.yml", "environment.yaml", "package-lock.json",
             "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "deno.json", "deno.jsonc",
             "deno.lock", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock", "*.gemspec",
             "composer.json", "composer.lock", "pubspec.yaml", "pubspec.lock", "mix.exs", "mix.lock", "build.gradle",
             "build.gradle.kts", "*gradle/libs.versions.toml", "pom.xml", "*.csproj", "*.fsproj", "*.vbproj",
             "packages.config", "Directory.Packages.props", "Podfile", "Podfile.lock", "Package.swift",
             "Package.resolved")


def is_manifest(path) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path if "/" in g else name, g) for g in MANIFESTS)
