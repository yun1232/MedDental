"""Verify retained upstream files and the workspace patch references without dependencies."""
import hashlib
import json
import re
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((Path(__file__).with_name("core-manifest.json")).read_text())
    problems = []
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file():
            problems.append(f"missing: {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            problems.append(f"modified: {name}")
    workspace = (root / "pnpm-workspace.yaml").read_text()
    for line in workspace.splitlines():
        if ": patches/" in line:
            patch = line.split(": ", 1)[1].strip()
            if not (root / patch).is_file():
                problems.append(f"missing dependency patch: {patch}")
    for name in ["apps/cli/src/bin.ts", "apps/web/package.json", "python/sdk-runtime/package.json",
                 "apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml"]:
        if not (root / name).is_file():
            problems.append(f"missing entry: {name}")
    refinement = json.loads(Path(__file__).with_name("medical-refinement.json").read_text())
    removed = {entry["name"] for batch in refinement["batches"] for entry in batch["packages"]}
    manifests = [root / "package.json", *root.glob("packages/*/*/package.json"),
                 *root.glob("apps/*/package.json"), root / "python/sdk-runtime/package.json"]
    for package in manifests:
        data = json.loads(package.read_text())
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name in removed.intersection(data.get(key, {})):
                problems.append(f"removed dependency remains: {package.relative_to(root)}: {name}")
        for name in data.get("dsh", {}).get("client", {}).get("external", []):
            if any(name == old or name.startswith(old + "/") for old in removed):
                problems.append(f"removed browser external remains: {package.relative_to(root)}: {name}")
        patches = data.get("dsh", {}).get("bundle", {}).get("patch", [])
        for patch in [patches] if isinstance(patches, str) else patches:
            if not (package.parent / patch).is_file():
                problems.append(f"missing bundle patch: {package.relative_to(root)}: {patch}")
    # Inspect runtime source and shipped compositions, including subpath imports.
    for area in (root / "packages", root / "apps"):
        for source in area.glob("**/src/**/*"):
            if "node_modules" in source.parts or not source.is_file() or source.suffix not in (".ts", ".tsx", ".mjs"):
                continue
            for name in re.findall(r"(?:from\s+|import\s*\(\s*)['\"]([^'\"]+)['\"]", source.read_text()):
                if any(name == old or name.startswith(old + "/") for old in removed):
                    problems.append(f"removed runtime import: {source.relative_to(root)}: {name}")
    for composition in [*root.glob("packages/*/*/*.yml"), *root.glob("packages/bundle/*/presets/*.yml")]:
        for name in re.findall(r"^\s*name:\s*['\"]?([^'\"\s]+)", composition.read_text(), re.M):
            if any(name == old or name.startswith(old + "/") for old in removed):
                problems.append(f"removed runtime plugin: {composition.relative_to(root)}: {name}")
    for batch in refinement["batches"]:
        for entry in batch["packages"]:
            if (root / entry["directory"]).exists():
                problems.append(f"removed package restored: {entry['directory']}")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"Saved source integrity: {len(manifest['files'])} tracked files match reviewed hashes; patch and entry references present.")


if __name__ == "__main__":
    main()
