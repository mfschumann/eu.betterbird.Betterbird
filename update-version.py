#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# Configuration
BETTERBIRD_REPO = "https://github.com/Betterbird/thunderbird-patches"
PACKAGE = "thunderbird"
PLATFORM = "linux-x86_64"
SOURCES_FILE = f"{PACKAGE}-sources.json"
APPDATA_FILE = "thunderbird-patches/metadata/eu.betterbird.Betterbird.140.appdata.xml"
MANIFEST_FILE = "eu.betterbird.Betterbird.yml"
DIST_FILE = "distribution.ini"
BUILD_DATE_FILE = ".build-date"
KNOWN_TAGS_FILE = ".known-tags"


from typing import Union

def run_cmd(cmd, cwd=None, check=True, capture=False, text=False) -> str:
    """Run a shell command."""
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, check=check,
        capture_output=capture, text=text
    )
    if capture:
        return result.stdout.strip()
    return ""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update Betterbird version",
        epilog=f"Example: {sys.argv[0]} 102.2.2-bb16\n"
               f"         {sys.argv[0]} 102 4d587481bc7dbca1ffc99cce319f84425fab7852"
    )
    parser.add_argument("version", help="Betterbird version (tag or major version)")
    parser.add_argument("commit", nargs="?", help="Betterbird commit hash (required if version is major version only)")
    parser.add_argument("-f", "--force", action="store_true", help="Skip version check from appdata.xml")
    parser.add_argument("-p", "--private-mirror", action="store_true", help="Replace upstream mirror with private mirror")
    return parser.parse_args()


def ensure_repo():
    """Clone or update the Betterbird repository."""
    if Path("thunderbird-patches").exists():
        run_cmd("git reset --hard HEAD", cwd="thunderbird-patches")
        run_cmd("git fetch", cwd="thunderbird-patches")
    else:
        run_cmd(f"git clone -n {BETTERBIRD_REPO} thunderbird-patches")


def get_commit(version=None, commit=None):
    """Checkout the specified commit and return its hash."""
    if commit:
        betterbird_commit = run_cmd(f"git rev-list -1 {commit}", cwd="thunderbird-patches", capture=True, text=True)
    else:
        betterbird_commit = run_cmd(f"git rev-list -1 {version}", cwd="thunderbird-patches", capture=True, text=True)
    run_cmd(f"git checkout {betterbird_commit}", cwd="thunderbird-patches")
    return betterbird_commit


def get_appdata_version():
    """Extract version from appdata.xml."""
    content = Path(APPDATA_FILE).read_text()
    match = re.search(r'<release version="([^"]+)"', content)
    if match:
        return match.group(1)
    return None


def get_base_url():
    """Extract base URL for sources from appdata.xml."""
    content = Path(APPDATA_FILE).read_text()
    match = re.search(r'<artifact type="source">\s*<location>([^<]+)</location>', content, re.DOTALL)
    if match:
        source_archive = match.group(1)
        return source_archive.rsplit("/source/", 1)[0]
    return None


def update_sources_file(base_url, betterbird_version):
    """Generate thunderbird-sources.json from SHA256SUMS."""
    # Get SHA256SUMS content
    sha256_output = run_cmd(f"curl -Ss '{base_url}/SHA256SUMS'", capture=True, text=True)
    
    entries = []
    source_archive = None
    
    for line in sha256_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        checksum, path = parts[0], parts[1].strip()
        
        if path.startswith("source/"):
            # Store source archive for last
            source_archive = {
                "type": "archive",
                "url": f"{base_url}/{path}",
                "sha256": checksum
            }
        elif path.startswith(f"{PLATFORM}/xpi/"):
            locale_file = Path(path).name
            locale = locale_file.rsplit(".", 1)[0]  # remove .xpi
            
            # Check if Betterbird has a patch for this locale
            major_version = betterbird_version.split(".")[0]
            patch_script = Path(f"thunderbird-patches/{major_version}/scripts/{locale}.sh")
            if patch_script.exists():
                entries.append({
                    "type": "file",
                    "url": f"{base_url}/{path}",
                    "sha256": checksum,
                    "dest": "langpacks/",
                    "dest-filename": f"langpack-{locale}@{PACKAGE}.mozilla.org.xpi"
                })
    
    # Write JSON array with source archive last
    with open(SOURCES_FILE, "w") as f:
        f.write("[\n")
        for entry in entries:
            f.write(f"    {json.dumps(entry, indent=8)[8:]},\n")
        f.write(f"    {json.dumps(source_archive, indent=8)[8:]}\n")
        f.write("]\n")


def update_manifest(betterbird_commit, source_spec, betterbird_version):
    """Update manifest YAML using PyYAML."""
    if yaml is None:
        raise ImportError("PyYAML is required for update_manifest. Install with: pip install pyyaml")
    
    with open(MANIFEST_FILE, "r") as f:
        manifest = yaml.safe_load(f)
    
    # Find the betterbird module
    for module in manifest.get("modules", []):
        if module.get("name") == "betterbird":
            for source in module.get("sources", []):
                if source.get("dest") == "thunderbird-patches":
                    source["commit"] = betterbird_commit
                    if source_spec == "tag":
                        source["tag"] = betterbird_version
                    else:
                        source.pop("tag", None)
                    break
            break
    
    with open(MANIFEST_FILE, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)


def update_distribution_ini(betterbird_commit):
    """Update version in distribution.ini."""
    short_commit = run_cmd(f"git rev-parse --short {betterbird_commit}", capture=True, text=True)
    sed_expr = f's/version=.*$/version={short_commit}/'
    run_cmd(f"sed -i '{sed_expr}' '{DIST_FILE}'")


def update_known_tags(betterbird_version):
    """Add version to .known-tags if not present."""
    known_tags = Path(KNOWN_TAGS_FILE)
    tags = []
    if known_tags.exists():
        tags = known_tags.read_text().splitlines()
    
    if betterbird_version not in tags:
        tags.append(betterbird_version)
        tags.sort()
        known_tags.write_text("\n".join(tags) + "\n")


def handle_private_mirror(betterbird_version):
    """Download sources to private mirror and update URLs."""
    # Extract tar.xz URLs from sources.json
    with open(SOURCES_FILE, "r") as f:
        data = json.load(f)
    
    urls = [entry["url"] for entry in data if entry["url"].endswith(".source.tar.xz")]
    
    # Download to private mirror
    for url in urls:
        run_cmd(f'ssh srv5dl "curl -C - --retry 5 --retry-all-errors -O --output-dir /srv/containers/dl {url}"')
    
    # Replace URLs in sources.json
    with open(SOURCES_FILE, "r") as f:
        content = f.read()
    
    new_content = re.sub(
        r'https://archive\.mozilla\.org/.*/([^/]+)\.source\.tar\.xz',
        r'https://dl.mfs.name/\1.source.tar.xz',
        content
    )
    
    with open(SOURCES_FILE, "w") as f:
        f.write(new_content)


def main():
    args = parse_args()
    
    betterbird_version = args.version
    betterbird_commit = args.commit
    
    # Determine source spec
    if betterbird_commit:
        source_spec = "commit"
    else:
        source_spec = "tag"
    
    print()
    if source_spec == "tag":
        print(f"Updating to TAG {betterbird_version}")
    else:
        print(f"Updating to COMMIT {betterbird_commit}")
    print(f" using Betterbird patches for Thunderbird {betterbird_version.split('.')[0]}")
    print()
    
    # Clone/update repo
    ensure_repo()
    
    # Checkout commit
    betterbird_commit = get_commit(betterbird_version if source_spec == "tag" else None, betterbird_commit)
    os.chdir("..")
    
    # Version check from appdata.xml
    if source_spec == "tag" and not args.force:
        appdata_version = get_appdata_version()
        if appdata_version and not betterbird_version.startswith(appdata_version):
            print(f"Betterbird version given on command line ({betterbird_version}) "
                  f"and version according to {APPDATA_FILE} ({appdata_version}) don't agree. Stopping.")
            print(f"Hint: This check can be skipped by passing the -f flag.")
            sys.exit(1)
    
    # Save build date
    tz = os.environ.get("TZ", "Europe/Berlin")
    build_date = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    Path(BUILD_DATE_FILE).write_text(build_date + "\n")
    
    # Get base URL
    base_url = get_base_url()
    if not base_url:
        print(f"Error: Could not extract base URL from {APPDATA_FILE}")
        sys.exit(1)
    
    # Update sources file
    update_sources_file(base_url, betterbird_version)
    
    # Update manifest
    update_manifest(betterbird_commit, source_spec, betterbird_version)
    
    # Update distribution.ini
    update_distribution_ini(betterbird_commit)
    
    # Update known tags
    if source_spec == "tag":
        update_known_tags(betterbird_version)
    
    # Private mirror handling
    if args.private_mirror:
        handle_private_mirror(betterbird_version)
    
    # Success message
    print(f"""The files were successfully updated to Betterbird {betterbird_version}.

You can commit the result by executing the following command:
git commit --message='Update to {betterbird_version}' -- '{SOURCES_FILE}' '{MANIFEST_FILE}' '{DIST_FILE}' '{BUILD_DATE_FILE}' '{KNOWN_TAGS_FILE}'
""")


if __name__ == "__main__":
    main()
